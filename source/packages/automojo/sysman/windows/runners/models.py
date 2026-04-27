"""
    Data models exchanged between Windows command runners, execution
    aspects, and the higher-level sysman features.

    The Windows runners share the same conceptual model as the Ubuntu
    and macOS runners: every execution returns a
    :class:`WindowsRunResult`, every in-flight command flows through a
    :class:`WindowsExecutionContext`, and SSH-shaped reliability
    metrics are tracked on a single
    :class:`WindowsSshReliabilityMetrics` dataclass that is also reused
    by the local runner for parity.

    The cross-cutting :class:`automojo.interfaces.icommandrunner.RunResult`
    is re-exported from this module under the alias ``RunResult`` so
    callers that only need the interface-level type can import from a
    single place.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from automojo.interfaces.icommandrunner import RunResult


__all__ = [
    "RunResult",
    "WindowsExecutionContext",
    "WindowsRunRecord",
    "WindowsRunResult",
    "WindowsSshErrorType",
    "WindowsSshReliabilityMetrics",
]


class WindowsSshErrorType(Enum):
    """
        Classification of SSH-related error categories used by the
        Windows SSH runner's reliability metrics.
    """
    NETWORK_ERROR = "network_error"
    AUTH_ERROR = "auth_error"
    HOST_KEY_ERROR = "host_key_error"
    TIMEOUT_ERROR = "timeout_error"
    CHANNEL_ERROR = "channel_error"
    UNKNOWN_ERROR = "unknown_error"


@dataclass
class WindowsRunResult:
    """
        Result of a single command executed by a Windows runner.

        The fields are a strict superset of
        :class:`automojo.interfaces.icommandrunner.RunResult` so that a
        Windows runner satisfies the cross-OS ``ICommandRunner``
        protocol and so that aspects can decorate a richer result
        without breaking interop.

        :param exit_code: Process exit code; ``None`` when the runner
                          could not produce one (e.g. a transport
                          error).
        :param stdout: Captured standard output.
        :param stderr: Captured standard error.
        :param command: Argument vector that was executed.
        :param timed_out: True if the command was killed by a timeout.
        :param dry_run: True if the result is synthetic.
        :param duration_ms: Wall-clock execution time in milliseconds.
        :param retry_count: Number of retry attempts made.
        :param transport: Identifier of the transport: ``"local"`` or
                          ``"ssh"``.
        :param elevation_blocked: True when the runner refused the
                                  command because it could not
                                  satisfy ``elevated=True``.  Windows
                                  has no shell-mediated UAC elevation
                                  so the runner surfaces this case as
                                  a typed flag in addition to a
                                  failure exit code.
    """

    exit_code: Optional[int]
    stdout: str
    stderr: str
    command: list[str]
    timed_out: bool = False
    dry_run: bool = False
    duration_ms: Optional[float] = None
    retry_count: int = 0
    transport: str = "local"
    elevation_blocked: bool = False

    @property
    def success(self) -> bool:
        """
            Whether the command completed cleanly (exit code zero and
            no timeout).
        """
        rtnval = (self.exit_code == 0) and (not self.timed_out)
        return rtnval


@dataclass
class WindowsRunRecord:
    """
        Persistence-friendly record of a single execution.  Used by
        the :class:`WindowsPersistenceAspect` JSON-line sink and by
        callers that want to replay a run after the fact.

        :param entry_id: Unique identifier for the record.
        :param command: Argument vector that was executed.
        :param cwd: Working directory of the command.
        :param env: Environment overrides supplied to the runner.
        :param transport: Transport identifier.
        :param elevated: Whether the command requested elevation.
        :param dry_run: Whether the record describes a dry-run.
        :param exit_code: Resulting exit code.
        :param stdout: Captured stdout.
        :param stderr: Captured stderr.
        :param duration_ms: Wall-clock duration in milliseconds.
        :param timestamp_start: Epoch seconds at start of execution.
        :param timestamp_end: Epoch seconds at end of execution.
    """

    entry_id: str
    command: list[str]
    cwd: Optional[str]
    env: Optional[dict[str, str]]
    transport: str
    elevated: bool
    dry_run: bool
    exit_code: Optional[int] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    duration_ms: Optional[float] = None
    timestamp_start: Optional[float] = None
    timestamp_end: Optional[float] = None


@dataclass
class WindowsSshReliabilityMetrics:
    """
        Cumulative reliability metrics for a single Windows runner
        instance.  The fields cover both local runners (where SSH-only
        fields stay at their defaults) and SSH runners.

        :param success_count: Number of successful executions.
        :param failure_count: Number of failed executions.
        :param timeout_count: Number of executions that timed out.
        :param connection_error_count: Number of connection-level
                                       errors.  Always zero for the
                                       local runner.
        :param retry_count: Number of retry attempts observed.
        :param total_duration_ms: Sum of all execution durations.
        :param min_duration_ms: Minimum observed duration.
        :param max_duration_ms: Maximum observed duration.
        :param avg_duration_ms: Average observed duration.
        :param last_error: String of the most recent error.
        :param last_error_type: Classification of the most recent
                                error.
        :param elevation_block_count: Number of executions that the
                                      runner refused due to a missing
                                      Administrator token.
    """

    success_count: int = 0
    failure_count: int = 0
    timeout_count: int = 0
    connection_error_count: int = 0
    retry_count: int = 0
    total_duration_ms: float = 0.0
    min_duration_ms: Optional[float] = None
    max_duration_ms: Optional[float] = None
    avg_duration_ms: Optional[float] = None
    last_error: Optional[str] = None
    last_error_type: Optional[WindowsSshErrorType] = None
    elevation_block_count: int = 0


@dataclass
class WindowsExecutionContext:
    """
        Context object passed to execution aspects describing one
        in-flight command.

        :param args: Argument vector to execute.
        :param elevated: Whether the command should run elevated.
        :param cwd: Working directory.
        :param env: Environment overrides.
        :param timeout: Timeout in seconds; ``None`` means no timeout.
        :param transport: Transport identifier.
        :param dry_run: Whether the execution is in dry-run mode.
        :param metadata: Free-form bag for aspect correlation data.
    """

    args: list[str]
    elevated: bool
    cwd: Optional[str]
    env: Optional[dict[str, str]]
    timeout: Optional[float]
    transport: str
    dry_run: bool
    metadata: dict[str, Any] = field(default_factory=dict)

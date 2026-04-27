"""
    Persistence aspect that records every command execution to a
    JSON-line sink.  The sink is any object exposing a ``write(line)``
    method; the canonical implementation is a file opened in append
    mode, but tests can supply an in-memory list adapter.
"""

import json
import logging
import time
import uuid

from dataclasses import dataclass
from typing import Any, Optional

from automojo.sysman.windows.runners.models import (
    WindowsExecutionContext,
    WindowsRunResult,
)


LOGGER = logging.getLogger(__name__)

_METADATA_ENTRY_ID_KEY = "windows_persistence.entry_id"
_METADATA_START_TIME_KEY = "windows_persistence.start_time"


@dataclass
class WindowsPersistenceAspectConfig:
    """
        Configuration for :class:`WindowsPersistenceAspect`.

        :param sink: Object exposing a ``write(line)`` method.  Each
                     completed execution produces one JSON line followed
                     by a newline character.
        :param log_environment: Whether to include the command env in
                                the recorded entry.  Defaults to
                                ``False`` to avoid leaking secrets.
        :param log_output: Whether to include captured stdout/stderr in
                           the recorded entry.
        :param fail_on_storage_error: When True, sink failures bubble.
                                      When False, failures are logged
                                      and execution continues.
    """

    sink: Any
    log_environment: bool = False
    log_output: bool = True
    fail_on_storage_error: bool = False


class WindowsPersistenceAspect:
    """
        Execution aspect that writes a JSON-line record per command
        execution to a configured sink.
    """

    def __init__(self, config: WindowsPersistenceAspectConfig) -> None:
        """
            :param config: Persistence configuration.
        """
        self._config: WindowsPersistenceAspectConfig = config
        self._enabled: bool = True
        return

    @property
    def enabled(self) -> bool:
        """
            Whether the aspect is active.
        """
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value
        return

    @property
    def config(self) -> WindowsPersistenceAspectConfig:
        """
            The active :class:`WindowsPersistenceAspectConfig`.
        """
        return self._config

    def before_execute(self, context: WindowsExecutionContext) -> None:
        """
            Tag the context with an entry id and start timestamp so
            that ``after_execute`` can correlate the completion record.
        """
        entry_id = str(uuid.uuid4())
        start_time = time.time()
        context.metadata[_METADATA_ENTRY_ID_KEY] = entry_id
        context.metadata[_METADATA_START_TIME_KEY] = start_time
        return

    def after_execute(
        self,
        context: WindowsExecutionContext,
        result: WindowsRunResult,
    ) -> WindowsRunResult:
        """
            Serialise the completed execution to the sink as a single
            JSON line.
        """
        entry_id = context.metadata.get(_METADATA_ENTRY_ID_KEY)
        if entry_id is None:
            LOGGER.warning("WindowsPersistenceAspect: missing entry id; skipping write")
            return result

        start_time = context.metadata.get(_METADATA_START_TIME_KEY)
        end_time = time.time()

        logged_env: Optional[dict[str, str]] = None
        if self._config.log_environment is True and context.env is not None:
            logged_env = dict(context.env)

        if self._config.log_output is True:
            stdout_text = result.stdout
            stderr_text = result.stderr
        else:
            stdout_text = ""
            stderr_text = ""

        record = {
            "entry_id": entry_id,
            "command": list(result.command),
            "cwd": context.cwd,
            "env": logged_env,
            "transport": context.transport,
            "elevated": context.elevated,
            "dry_run": context.dry_run,
            "exit_code": result.exit_code,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "duration_ms": result.duration_ms,
            "timestamp_start": start_time,
            "timestamp_end": end_time,
            "timed_out": result.timed_out,
            "retry_count": result.retry_count,
            "elevation_blocked": result.elevation_blocked,
        }

        line = json.dumps(record, default=str)
        try:
            self._config.sink.write(line + "\n")
        except Exception as ex:
            if self._config.fail_on_storage_error is True:
                raise
            LOGGER.warning("WindowsPersistenceAspect: sink write failed: %s", ex)

        return result

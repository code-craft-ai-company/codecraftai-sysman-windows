"""
    Windows container-management feature.

    Implements the cross-OS container-management contract for Windows
    by wrapping the ``docker.exe`` (default — Docker Desktop) or
    ``podman.exe`` / ``wsl podman`` CLI through the injected
    :class:`ICommandRunner`.  The engine is selected at construction
    time via :class:`WindowsContainerManagerConfig.engine`.

    Concept mapping:
        Windows hosts container engines two ways:

        * **Docker Desktop** — installs ``docker.exe`` on PATH and
          runs containers inside a WSL2 distro (Linux containers) or
          via the native Windows containerd backend (Windows
          containers).  This manager invokes ``docker.exe`` directly
          and is agnostic about the backend; the engine is the same
          binary in both cases.
        * **Podman on Windows** — Podman has a native Windows port
          (`podman.exe`) that drives a WSL2-hosted machine via
          ``podman machine``.  The cross-OS contract enumerates this
          as a separate engine; this manager surfaces it as
          :attr:`WindowsContainerEngine.PODMAN_WSL`.  Skeleton-only
          today — the verbs are wired but Podman-on-Windows in
          production is rare.

        See ``specs/3-container-management/design.md`` for the full
        engine-mapping table.

    Volume mounts on Windows:
        Docker on Windows accepts volume strings of the form
        ``-v 'C:\\host\\path:/container/path'``.  The host portion
        uses native Windows paths (the engine internally translates
        them through WSL2 / the Linux VM).  Coordinators that build
        :class:`WindowsContainerSpec.volumes` must pass the
        Windows-style host portion as-is; the manager does not
        rewrite paths.  The runner passes argv directly to the
        engine, so no extra quoting is required from the manager —
        the engine itself knows how to parse the colon separator
        when the host portion is a drive-letter path.

    Engine selection:
        :class:`WindowsContainerEngine` carries ``DOCKER`` (default)
        and ``PODMAN_WSL`` (the WSL2-hosted Podman flavor).  The
        configured engine determines argv[0] and, for
        ``PODMAN_WSL``, the leading ``wsl`` prefix when the binary
        is not on the Windows PATH directly.

    Idempotency:
        Same as the Ubuntu / macOS siblings.

    Elevation policy:
        Docker Desktop on Windows runs as the desktop user; no
        elevation is normally required.  The ``elevated`` flag on
        the config exists for cross-OS-contract parity and for
        Windows-Container backends that require an elevated token
        to talk to the rootful daemon.  Reads never elevate.
"""

import json
import os

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from ccai.sysman.common.featurebase import (
    ActionStatus,
    BaseFeatureManager,
    FeatureActionResult,
    FeatureConfig,
)


# ----------------------------------------------------------------------
# Module-level constants and engine binaries
# ----------------------------------------------------------------------


_DOCKER_EXE = "docker.exe"
_PODMAN_EXE = "podman.exe"
_WSL_EXE = "wsl.exe"
_PODMAN_TOKEN = "podman"


# Default tail count when the caller does not pass one.
_DEFAULT_LOG_LINES = 100


# Default stop timeout in seconds.
_DEFAULT_STOP_TIMEOUT = 10


# ----------------------------------------------------------------------
# Aliases / config / result types
# ----------------------------------------------------------------------


WindowsContainerActionStatus = ActionStatus


class WindowsContainerEngine(Enum):
    """
        Supported container engines on Windows.

        ``DOCKER`` invokes ``docker.exe`` (Docker Desktop on PATH).
        ``PODMAN_WSL`` invokes Podman through WSL2 — either via the
        native ``podman.exe`` Windows port or via ``wsl.exe podman``
        when the Windows port is not installed but a Linux distro
        with podman is available.  The
        :attr:`WindowsContainerManagerConfig.use_wsl_for_podman`
        flag selects the indirection.
    """
    DOCKER = "docker"
    PODMAN_WSL = "podman_wsl"


@dataclass
class WindowsContainerManagerConfig(FeatureConfig):
    """
        Configuration for :class:`WindowsContainerManager`.  Inherits
        ``dry_run`` and ``elevated`` from :class:`FeatureConfig`.

        :param engine: The container engine to invoke.  Defaults to
                       :attr:`WindowsContainerEngine.DOCKER`.
        :param force_pull: When True, ``pull_image`` always invokes
                           ``<engine> pull`` even when the reference
                           is already present locally.
        :param default_stop_timeout: Default ``-t`` value for
                                     ``<engine> stop``.
        :param use_wsl_for_podman: When True (and ``engine`` is
                                   :attr:`WindowsContainerEngine.PODMAN_WSL`)
                                   the manager prefixes every podman
                                   invocation with ``wsl podman``;
                                   when False it invokes
                                   ``podman.exe`` directly.  Defaults
                                   to True since the native port is
                                   rare in production.
        :param docker_executable: The ``docker.exe`` binary token.
                                  Defaults to ``"docker.exe"``
                                  (PATH-resolved).
    """
    engine: WindowsContainerEngine = WindowsContainerEngine.DOCKER
    force_pull: bool = False
    default_stop_timeout: int = _DEFAULT_STOP_TIMEOUT
    use_wsl_for_podman: bool = True
    docker_executable: str = _DOCKER_EXE


@dataclass
class WindowsContainerActionResult(FeatureActionResult):
    """
        Result of a container-management write operation.

        :param engine: The engine the manager used.
        :param container_id: Container id, when applicable.
        :param image: Image reference, when applicable.
        :param target: Generic target identifier.
        :param message: Human-readable detail for ``SKIPPED`` /
                        ``FAILED``.
    """
    engine: Optional[WindowsContainerEngine] = None
    container_id: Optional[str] = None
    image: Optional[str] = None
    target: Optional[str] = None
    message: Optional[str] = None


@dataclass
class WindowsContainerExecResult(FeatureActionResult):
    """
        Result of :meth:`WindowsContainerManager.exec_in_container`.

        :param engine: The engine the manager used.
        :param container_id: The container the command ran in.
        :param command_argv: The argv passed inside the container.
        :param output: Captured stdout from the inner command.
    """
    engine: Optional[WindowsContainerEngine] = None
    container_id: Optional[str] = None
    command_argv: Optional[list] = None
    output: Optional[str] = None


# ----------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------


@dataclass
class WindowsImageInfo:
    """
        Information about a single image reported by
        ``<engine> images --format '{{json .}}'``.

        :param id: Image id.
        :param repository: Repository portion or ``None`` for dangling.
        :param tag: Tag portion or ``None`` for ``<none>``.
        :param digest: Image digest when reported, else ``None``.
        :param size_bytes: On-disk size in bytes when parseable.
        :param created: Free-form creation timestamp string.
    """
    id: str
    repository: Optional[str] = None
    tag: Optional[str] = None
    digest: Optional[str] = None
    size_bytes: Optional[int] = None
    created: str = ""


@dataclass
class WindowsContainerSpec:
    """
        Specification for :meth:`WindowsContainerManager.create_container`.

        :param name: Container name (the ``--name`` argument).
        :param image: Image reference.
        :param argv: Optional command override.
        :param env: Environment-variable overrides, applied as
                    ``-e KEY=VALUE``.
        :param volumes: Volume mount strings.  On Windows these use
                        the form
                        ``"C:\\host\\path:/container/path"`` for
                        Linux-container backends; native Windows
                        Containers accept
                        ``"C:\\host:C:\\container"``.  The manager
                        passes the token through unchanged.
        :param ports: Port-publication strings.
        :param networks: Network names.
        :param restart_policy: Restart-policy token.
        :param working_dir: ``--workdir`` value.
        :param user: ``--user`` value.
        :param detach: Recorded for cross-OS-contract parity.
    """
    name: str
    image: str
    argv: Optional[list] = None
    env: Optional[dict] = None
    volumes: Optional[list] = None
    ports: Optional[list] = None
    networks: Optional[list] = None
    restart_policy: Optional[str] = None
    working_dir: Optional[str] = None
    user: Optional[str] = None
    detach: bool = True


@dataclass
class WindowsContainerInfo:
    """
        Summary of a single container.

        :param id: Container id.
        :param name: Container name.
        :param image: Image reference.
        :param status: Native human-readable status string.
        :param state: Lifecycle state token (``running``, ``exited``,
                      ``created``, ``paused``, ``restarting``,
                      ``dead``).
        :param created: Creation time string.
        :param started: Start time when running, else ``None``.
        :param ports: Published port mapping strings.
        :param mounts: Mount-target strings.
        :param command: The container's command argv.
        :param engine: The engine that reported the container.
    """
    id: str
    name: str
    image: str
    status: str = ""
    state: str = ""
    created: str = ""
    started: Optional[str] = None
    ports: list = field(default_factory=list)
    mounts: list = field(default_factory=list)
    command: list = field(default_factory=list)
    engine: Optional[WindowsContainerEngine] = None


@dataclass
class WindowsContainerLogs:
    """
        Captured log output from a container.

        :param container_id: The source container id.
        :param lines: One entry per log line.
        :param truncated: True when the manager applied a tail limit.
    """
    container_id: str
    lines: list = field(default_factory=list)
    truncated: bool = False


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------


class WindowsContainerQueryError(Exception):
    """
        Raised when a container read fails for a reason other than
        the target simply being absent.
    """

    def __init__(
        self,
        *,
        command: list,
        exit_code: Optional[int],
        stderr: str,
    ) -> None:
        """
            :param command: The argv that failed.
            :param exit_code: The runner's exit code.
            :param stderr: The runner's captured stderr.
        """
        self.command: list = list(command)
        self.exit_code = exit_code
        self.stderr = stderr

        err_msg_lines = ["Container query command failed."]
        err_msg_lines.append("    COMMAND: {}".format(" ".join(command)))
        err_msg_lines.append("    EXIT_CODE: {}".format(exit_code))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class WindowsContainerEngineNotFoundError(Exception):
    """
        Raised when the configured engine binary is not available.
    """

    def __init__(
        self,
        *,
        engine: WindowsContainerEngine,
        stderr: str,
    ) -> None:
        """
            :param engine: The engine that could not be invoked.
            :param stderr: The runner's captured stderr.
        """
        self.engine = engine
        self.stderr = stderr

        err_msg_lines = ["Container engine not found on host."]
        err_msg_lines.append("    ENGINE: {}".format(engine.value))
        err_msg_lines.append("    STDERR: {}".format(stderr))
        err_msg = os.linesep.join(err_msg_lines)
        super().__init__(err_msg)
        return


class WindowsContainerNotFoundError(Exception):
    """
        Reserved for cross-OS-contract parity.  Not raised today.
    """


# ----------------------------------------------------------------------
# WindowsContainerManager
# ----------------------------------------------------------------------


class WindowsContainerManager(BaseFeatureManager):
    """
        Windows container manager.  Every verb is dispatched through
        the injected :class:`ICommandRunner`.  See the module
        docstring for the engine selection model and elevation
        policy.
    """

    result_class = WindowsContainerActionResult
    status_class = WindowsContainerActionStatus
    feature_name = "container-management"

    # ------------------------------------------------------------------
    # Read verbs — images
    # ------------------------------------------------------------------

    def list_images(self) -> list:
        """
            Enumerate images via
            ``<engine> images --format '{{json .}}'``.
        """
        prefix = self._engine_prefix()
        command = list(prefix) + ["images", "--format", "{{json .}}"]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: list = []
            return rtnval
        rtnval = _parse_image_lines(output=run_result.stdout)
        return rtnval

    # ------------------------------------------------------------------
    # Read verbs — containers
    # ------------------------------------------------------------------

    def list_containers(
        self,
        *,
        all: bool = False,
    ) -> list:
        """
            Enumerate containers via
            ``<engine> ps [-a] --format '{{json .}}'``.
        """
        engine_value = self._typed_config().engine
        prefix = self._engine_prefix()
        command = list(prefix) + ["ps"]
        if all:
            command.append("-a")
        command.extend(["--format", "{{json .}}"])
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: list = []
            return rtnval
        rtnval = _parse_container_ps_lines(
            output=run_result.stdout, engine=engine_value,
        )
        return rtnval

    def inspect_container(
        self,
        container_id: str,
    ) -> Optional[WindowsContainerInfo]:
        """
            Inspect a single container via ``<engine> inspect``.
        """
        record = self._inspect_raw(target=container_id)
        if record is None:
            rtnval: Optional[WindowsContainerInfo] = None
            return rtnval
        engine_value = self._typed_config().engine
        rtnval = _build_info_from_inspect(record=record, engine=engine_value)
        return rtnval

    def get_container_logs(
        self,
        container_id: str,
        lines: int = _DEFAULT_LOG_LINES,
    ) -> WindowsContainerLogs:
        """
            Fetch up to ``lines`` log lines via
            ``<engine> logs --tail <lines> <id>``.
        """
        prefix = self._engine_prefix()
        command = list(prefix) + [
            "logs", "--tail", str(lines), container_id,
        ]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            empty = WindowsContainerLogs(
                container_id=container_id,
                lines=[],
                truncated=True,
            )
            return empty
        log_lines = _split_log_lines(output=run_result.stdout)
        rtnval = WindowsContainerLogs(
            container_id=container_id,
            lines=log_lines,
            truncated=True,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs — images
    # ------------------------------------------------------------------

    def pull_image(self, ref: str) -> WindowsContainerActionResult:
        """
            Pull ``ref`` via ``<engine> pull <ref>``.
        """
        engine_value = self._typed_config().engine
        prefix = self._engine_prefix()
        command = list(prefix) + ["pull", ref]

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsContainerActionStatus.DRY_RUN,
                command=command,
                target=ref,
                image=ref,
                engine=engine_value,
            )
            return dry

        force = self._typed_config().force_pull
        if not force:
            already_present = self._image_present(ref=ref)
            if already_present:
                skipped = self._build_result(
                    status=WindowsContainerActionStatus.SKIPPED,
                    command=command,
                    target=ref,
                    image=ref,
                    engine=engine_value,
                    message="image {} already present".format(ref),
                )
                return skipped

        rtnval = self._dispatch_container_write(
            command=command,
            target=ref,
            image=ref,
            engine=engine_value,
        )
        return rtnval

    def remove_image(self, ref: str) -> WindowsContainerActionResult:
        """
            Remove ``ref`` via ``<engine> rmi <ref>``.
        """
        engine_value = self._typed_config().engine
        prefix = self._engine_prefix()
        command = list(prefix) + ["rmi", ref]

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsContainerActionStatus.DRY_RUN,
                command=command,
                target=ref,
                image=ref,
                engine=engine_value,
            )
            return dry

        already_present = self._image_present(ref=ref)
        if not already_present:
            skipped = self._build_result(
                status=WindowsContainerActionStatus.SKIPPED,
                command=command,
                target=ref,
                image=ref,
                engine=engine_value,
                message="image {} not present".format(ref),
            )
            return skipped

        rtnval = self._dispatch_container_write(
            command=command,
            target=ref,
            image=ref,
            engine=engine_value,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Write verbs — containers
    # ------------------------------------------------------------------

    def create_container(
        self,
        spec: WindowsContainerSpec,
    ) -> WindowsContainerActionResult:
        """
            Create a container via ``<engine> create --name <name>
            [<flags...>] <image> [<argv...>]``.
        """
        engine_value = self._typed_config().engine
        prefix = self._engine_prefix()
        command = _build_create_argv(prefix=prefix, spec=spec)

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsContainerActionStatus.DRY_RUN,
                command=command,
                target=spec.name,
                image=spec.image,
                engine=engine_value,
            )
            return dry

        existing = self._inspect_raw(target=spec.name)
        if existing is not None:
            existing_image = _extract_image_from_inspect(record=existing)
            if existing_image == spec.image:
                skipped = self._build_result(
                    status=WindowsContainerActionStatus.SKIPPED,
                    command=command,
                    target=spec.name,
                    image=spec.image,
                    engine=engine_value,
                    container_id=existing.get("Id"),
                    message=(
                        "container {} already exists with image {}".format(
                            spec.name, spec.image,
                        )
                    ),
                )
                return skipped

        result = self._dispatch_container_write(
            command=command,
            target=spec.name,
            image=spec.image,
            engine=engine_value,
        )
        if result.status == WindowsContainerActionStatus.SUCCESS:
            new_id = result.stdout.strip() if result.stdout is not None else ""
            if len(new_id) > 0:
                result.container_id = new_id
        return result

    def start_container(
        self,
        container_id: str,
    ) -> WindowsContainerActionResult:
        """
            Start a container via ``<engine> start <id>``.
        """
        engine_value = self._typed_config().engine
        prefix = self._engine_prefix()
        command = list(prefix) + ["start", container_id]

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsContainerActionStatus.DRY_RUN,
                command=command,
                target=container_id,
                container_id=container_id,
                engine=engine_value,
            )
            return dry

        existing = self._inspect_raw(target=container_id)
        if existing is not None:
            running = _extract_running(record=existing)
            if running:
                skipped = self._build_result(
                    status=WindowsContainerActionStatus.SKIPPED,
                    command=command,
                    target=container_id,
                    container_id=container_id,
                    engine=engine_value,
                    message=(
                        "container {} is already running".format(
                            container_id,
                        )
                    ),
                )
                return skipped

        rtnval = self._dispatch_container_write(
            command=command,
            target=container_id,
            container_id=container_id,
            engine=engine_value,
        )
        return rtnval

    def stop_container(
        self,
        container_id: str,
    ) -> WindowsContainerActionResult:
        """
            Stop a container via ``<engine> stop -t <timeout> <id>``.
        """
        engine_value = self._typed_config().engine
        timeout = self._typed_config().default_stop_timeout
        prefix = self._engine_prefix()
        command = list(prefix) + [
            "stop", "-t", str(timeout), container_id,
        ]

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsContainerActionStatus.DRY_RUN,
                command=command,
                target=container_id,
                container_id=container_id,
                engine=engine_value,
            )
            return dry

        existing = self._inspect_raw(target=container_id)
        if existing is None:
            skipped = self._build_result(
                status=WindowsContainerActionStatus.SKIPPED,
                command=command,
                target=container_id,
                container_id=container_id,
                engine=engine_value,
                message="container {} does not exist".format(container_id),
            )
            return skipped

        running = _extract_running(record=existing)
        if not running:
            skipped = self._build_result(
                status=WindowsContainerActionStatus.SKIPPED,
                command=command,
                target=container_id,
                container_id=container_id,
                engine=engine_value,
                message=(
                    "container {} is already stopped".format(container_id)
                ),
            )
            return skipped

        rtnval = self._dispatch_container_write(
            command=command,
            target=container_id,
            container_id=container_id,
            engine=engine_value,
        )
        return rtnval

    def remove_container(
        self,
        container_id: str,
    ) -> WindowsContainerActionResult:
        """
            Remove a container via ``<engine> rm -f <id>``.
        """
        engine_value = self._typed_config().engine
        prefix = self._engine_prefix()
        command = list(prefix) + ["rm", "-f", container_id]

        if self._config.dry_run:
            dry = self._build_result(
                status=WindowsContainerActionStatus.DRY_RUN,
                command=command,
                target=container_id,
                container_id=container_id,
                engine=engine_value,
            )
            return dry

        existing = self._inspect_raw(target=container_id)
        if existing is None:
            skipped = self._build_result(
                status=WindowsContainerActionStatus.SKIPPED,
                command=command,
                target=container_id,
                container_id=container_id,
                engine=engine_value,
                message="container {} does not exist".format(container_id),
            )
            return skipped

        rtnval = self._dispatch_container_write(
            command=command,
            target=container_id,
            container_id=container_id,
            engine=engine_value,
        )
        return rtnval

    def exec_in_container(
        self,
        container_id: str,
        argv: list,
    ) -> WindowsContainerExecResult:
        """
            Run ``argv`` inside the container via
            ``<engine> exec <id> <argv...>``.
        """
        engine_value = self._typed_config().engine
        prefix = self._engine_prefix()
        command = list(prefix) + ["exec", container_id] + list(argv)

        if self._config.dry_run:
            dry = WindowsContainerExecResult(
                status=WindowsContainerActionStatus.DRY_RUN,
                command=list(command),
                detail=container_id,
                engine=engine_value,
                container_id=container_id,
                command_argv=list(argv),
                output=None,
            )
            return dry

        run_result = self._runner.run(
            list(command), elevated=self._config.elevated,
        )
        if run_result.exit_code == 0:
            mapped_status = WindowsContainerActionStatus.SUCCESS
        else:
            mapped_status = WindowsContainerActionStatus.FAILED

        rtnval = WindowsContainerExecResult(
            status=mapped_status,
            command=list(command),
            detail=container_id,
            exit_code=run_result.exit_code,
            stdout=run_result.stdout,
            stderr=run_result.stderr,
            engine=engine_value,
            container_id=container_id,
            command_argv=list(argv),
            output=run_result.stdout,
        )
        return rtnval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _typed_config(self) -> WindowsContainerManagerConfig:
        """
            Return ``self._config`` typed as
            :class:`WindowsContainerManagerConfig`.
        """
        if isinstance(self._config, WindowsContainerManagerConfig):
            rtnval = self._config
        else:
            rtnval = WindowsContainerManagerConfig(
                dry_run=self._config.dry_run,
                elevated=self._config.elevated,
            )
        return rtnval

    def _engine_prefix(self) -> list:
        """
            Return the argv prefix for the configured engine.

            For ``DOCKER`` this is ``[docker_executable]``; for
            ``PODMAN_WSL`` it is either ``["wsl.exe", "podman"]``
            (when ``use_wsl_for_podman`` is True) or
            ``["podman.exe"]``.
        """
        cfg = self._typed_config()
        if cfg.engine == WindowsContainerEngine.PODMAN_WSL:
            if cfg.use_wsl_for_podman:
                rtnval = [_WSL_EXE, _PODMAN_TOKEN]
            else:
                rtnval = [_PODMAN_EXE]
            return rtnval
        rtnval = [cfg.docker_executable]
        return rtnval

    def _image_present(self, *, ref: str) -> bool:
        """
            Probe ``<engine> images --filter 'reference=<ref>'``.
        """
        prefix = self._engine_prefix()
        filter_token = "reference={}".format(ref)
        command = list(prefix) + [
            "images",
            "--filter", filter_token,
            "--format", "{{json .}}",
        ]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval = False
            return rtnval
        stripped = run_result.stdout.strip()
        rtnval = len(stripped) > 0
        return rtnval

    def _inspect_raw(self, *, target: str) -> Optional[dict]:
        """
            Run ``<engine> inspect <target>`` and return element 0
            of the JSON-array response, or ``None``.
        """
        prefix = self._engine_prefix()
        command = list(prefix) + ["inspect", target]
        run_result = self._runner.run(list(command), elevated=False)
        if run_result.exit_code != 0:
            rtnval: Optional[dict] = None
            return rtnval
        rtnval = _parse_inspect_first(output=run_result.stdout)
        return rtnval

    def _dispatch_container_write(
        self,
        *,
        command: list,
        target: Optional[str] = None,
        image: Optional[str] = None,
        container_id: Optional[str] = None,
        engine: Optional[WindowsContainerEngine] = None,
    ) -> WindowsContainerActionResult:
        """
            Dispatch a write command and wrap the result.
        """
        rtnval = self._dispatch(
            command=command,
            detail=target,
            operation="container_write",
            target=target,
            image=image,
            container_id=container_id,
            engine=engine,
        )
        return rtnval

    def _build_result(
        self,
        *,
        status: WindowsContainerActionStatus,
        command: list,
        target: Optional[str] = None,
        image: Optional[str] = None,
        container_id: Optional[str] = None,
        engine: Optional[WindowsContainerEngine] = None,
        message: Optional[str] = None,
        stderr: Optional[str] = None,
    ) -> WindowsContainerActionResult:
        """
            Construct a :class:`WindowsContainerActionResult` without
            invoking the runner.
        """
        if message is not None:
            detail_value: Optional[str] = message
        else:
            detail_value = target
        rtnval = WindowsContainerActionResult(
            status=status,
            detail=detail_value,
            command=list(command),
            target=target,
            image=image,
            container_id=container_id,
            engine=engine,
            message=message,
            stderr=stderr,
        )
        return rtnval


# ----------------------------------------------------------------------
# Module-level argv builders
# ----------------------------------------------------------------------


def _build_create_argv(
    *,
    prefix: list,
    spec: WindowsContainerSpec,
) -> list:
    """
        Build the argv for ``<engine> create``.
    """
    command = list(prefix) + ["create", "--name", spec.name]

    if spec.restart_policy is not None:
        restart_token = "--restart={}".format(spec.restart_policy)
        command.append(restart_token)

    if spec.user is not None:
        command.extend(["--user", spec.user])

    if spec.working_dir is not None:
        command.extend(["--workdir", spec.working_dir])

    if spec.env is not None:
        for key in spec.env:
            value = spec.env[key]
            kv_token = "{}={}".format(key, value)
            command.extend(["-e", kv_token])

    if spec.volumes is not None:
        for volume_token in spec.volumes:
            command.extend(["-v", volume_token])

    if spec.ports is not None:
        for port_token in spec.ports:
            command.extend(["-p", port_token])

    if spec.networks is not None:
        for network_name in spec.networks:
            command.extend(["--network", network_name])

    command.append(spec.image)

    if spec.argv is not None and len(spec.argv) > 0:
        command.extend(list(spec.argv))

    return command


# ----------------------------------------------------------------------
# Module-level parsers
# ----------------------------------------------------------------------


def _parse_image_lines(*, output: str) -> list:
    """
        Parse one JSON object per line into :class:`WindowsImageInfo`
        rows.
    """
    rtnval: list = []
    stripped = output.strip()
    if len(stripped) == 0:
        return rtnval
    for raw_line in stripped.splitlines():
        line_text = raw_line.strip()
        if len(line_text) == 0:
            continue
        try:
            entry = json.loads(line_text)
        except (ValueError, TypeError):
            continue
        if not isinstance(entry, dict):
            continue
        image_id = entry.get("ID", entry.get("Id", ""))
        repo_value = entry.get("Repository")
        if isinstance(repo_value, str) and repo_value not in ("", "<none>"):
            repository: Optional[str] = repo_value
        else:
            repository = None
        tag_value = entry.get("Tag")
        if isinstance(tag_value, str) and tag_value not in ("", "<none>"):
            tag: Optional[str] = tag_value
        else:
            tag = None
        digest_value = entry.get("Digest")
        if (
            isinstance(digest_value, str)
            and digest_value not in ("", "<none>")
        ):
            digest: Optional[str] = digest_value
        else:
            digest = None
        size_bytes = _parse_image_size(raw_value=entry.get("Size"))
        created_value = entry.get("CreatedAt", entry.get("Created", ""))
        if not isinstance(created_value, str):
            created_value = str(created_value)
        info = WindowsImageInfo(
            id=image_id,
            repository=repository,
            tag=tag,
            digest=digest,
            size_bytes=size_bytes,
            created=created_value,
        )
        rtnval.append(info)
    return rtnval


def _parse_image_size(*, raw_value) -> Optional[int]:
    """
        Parse the engine's ``Size`` field tolerating int and string.
    """
    if raw_value is None:
        rtnval: Optional[int] = None
        return rtnval
    if isinstance(raw_value, int):
        rtnval = raw_value
        return rtnval
    if not isinstance(raw_value, str):
        rtnval = None
        return rtnval
    text = raw_value.strip()
    if len(text) == 0:
        rtnval = None
        return rtnval
    try:
        rtnval = int(text)
    except ValueError:
        rtnval = None
    return rtnval


def _parse_container_ps_lines(
    *,
    output: str,
    engine: WindowsContainerEngine,
) -> list:
    """
        Parse ``<engine> ps --format '{{json .}}'`` output.
    """
    rtnval: list = []
    stripped = output.strip()
    if len(stripped) == 0:
        return rtnval
    for raw_line in stripped.splitlines():
        line_text = raw_line.strip()
        if len(line_text) == 0:
            continue
        try:
            entry = json.loads(line_text)
        except (ValueError, TypeError):
            continue
        if not isinstance(entry, dict):
            continue
        container_id = entry.get("ID", entry.get("Id", ""))
        names_field = entry.get("Names")
        if isinstance(names_field, list) and len(names_field) > 0:
            name = names_field[0]
        elif isinstance(names_field, str) and len(names_field) > 0:
            name = names_field
        else:
            name = entry.get("Name", "")
        image = entry.get("Image", "")
        status = entry.get("Status", "")
        state = entry.get("State", "")
        created = entry.get("CreatedAt", entry.get("Created", ""))
        if not isinstance(created, str):
            created = str(created)
        ports_value = entry.get("Ports")
        if isinstance(ports_value, list):
            ports_list = list(ports_value)
        elif isinstance(ports_value, str) and len(ports_value) > 0:
            ports_list = [token.strip() for token in ports_value.split(",")]
        else:
            ports_list = []
        mounts_value = entry.get("Mounts")
        if isinstance(mounts_value, list):
            mounts_list = list(mounts_value)
        elif isinstance(mounts_value, str) and len(mounts_value) > 0:
            mounts_list = [token.strip() for token in mounts_value.split(",")]
        else:
            mounts_list = []
        command_value = entry.get("Command")
        if isinstance(command_value, list):
            command_list = list(command_value)
        elif isinstance(command_value, str) and len(command_value) > 0:
            command_list = [command_value]
        else:
            command_list = []
        info = WindowsContainerInfo(
            id=container_id,
            name=name,
            image=image,
            status=status,
            state=state,
            created=created,
            started=None,
            ports=ports_list,
            mounts=mounts_list,
            command=command_list,
            engine=engine,
        )
        rtnval.append(info)
    return rtnval


def _parse_inspect_first(*, output: str) -> Optional[dict]:
    """
        Parse ``<engine> inspect`` output (a JSON array) and return
        element 0.
    """
    stripped = output.strip()
    if len(stripped) == 0:
        rtnval: Optional[dict] = None
        return rtnval
    try:
        data = json.loads(stripped)
    except (ValueError, TypeError):
        rtnval = None
        return rtnval
    if not isinstance(data, list):
        rtnval = None
        return rtnval
    if len(data) == 0:
        rtnval = None
        return rtnval
    head = data[0]
    if not isinstance(head, dict):
        rtnval = None
        return rtnval
    rtnval = head
    return rtnval


def _build_info_from_inspect(
    *,
    record: dict,
    engine: WindowsContainerEngine,
) -> WindowsContainerInfo:
    """
        Build a :class:`WindowsContainerInfo` from a single inspect
        record.
    """
    container_id = record.get("Id", record.get("ID", ""))
    raw_name = record.get("Name", "")
    if isinstance(raw_name, str) and raw_name.startswith("/"):
        name = raw_name[1:]
    else:
        name = raw_name
    config_section = record.get("Config")
    if isinstance(config_section, dict):
        image = config_section.get("Image", "")
        cmd_value = config_section.get("Cmd")
        if isinstance(cmd_value, list):
            command_list = list(cmd_value)
        else:
            command_list = []
    else:
        image = ""
        command_list = []
    state_section = record.get("State")
    if isinstance(state_section, dict):
        state_status = state_section.get("Status", "")
        running_flag = bool(state_section.get("Running", False))
        started_at = state_section.get("StartedAt")
        if isinstance(started_at, str) and len(started_at) > 0:
            started_value: Optional[str] = started_at
        else:
            started_value = None
    else:
        state_status = ""
        running_flag = False
        started_value = None
    if running_flag:
        status_text = "Up"
    else:
        status_text = state_status
    created = record.get("Created", "")
    if not isinstance(created, str):
        created = str(created)
    network_section = record.get("NetworkSettings")
    ports_list: list = []
    if isinstance(network_section, dict):
        ports_section = network_section.get("Ports")
        if isinstance(ports_section, dict):
            for port_key in ports_section:
                ports_list.append(port_key)
    mounts_section = record.get("Mounts")
    mounts_list: list = []
    if isinstance(mounts_section, list):
        for mount_entry in mounts_section:
            if not isinstance(mount_entry, dict):
                continue
            destination = mount_entry.get("Destination", "")
            if isinstance(destination, str) and len(destination) > 0:
                mounts_list.append(destination)
    rtnval = WindowsContainerInfo(
        id=container_id,
        name=name,
        image=image,
        status=status_text,
        state=state_status,
        created=created,
        started=started_value,
        ports=ports_list,
        mounts=mounts_list,
        command=command_list,
        engine=engine,
    )
    return rtnval


def _extract_image_from_inspect(*, record: dict) -> str:
    """
        Pull the configured image reference from an inspect record.
    """
    config_section = record.get("Config")
    if isinstance(config_section, dict):
        image_value = config_section.get("Image")
        if isinstance(image_value, str) and len(image_value) > 0:
            rtnval = image_value
            return rtnval
    fallback = record.get("Image", "")
    if isinstance(fallback, str):
        rtnval = fallback
    else:
        rtnval = ""
    return rtnval


def _extract_running(*, record: dict) -> bool:
    """
        Return True when ``State.Running`` is True.
    """
    state_section = record.get("State")
    if not isinstance(state_section, dict):
        rtnval = False
        return rtnval
    rtnval = bool(state_section.get("Running", False))
    return rtnval


def _split_log_lines(*, output: str) -> list:
    """
        Split engine log output into one entry per line.
    """
    rtnval: list = []
    if output is None:
        return rtnval
    text = output
    if text.endswith(os.linesep):
        text = text[:-len(os.linesep)]
    elif text.endswith("\n"):
        text = text[:-1]
    if len(text) == 0:
        return rtnval
    rtnval = text.split("\n")
    return rtnval

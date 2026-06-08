"""
    Tests for :class:`WindowsContainerManager`.

    All tests use a ``_MockCommandRunner`` that records every ``run``
    call and returns pre-seeded :class:`RunResult` values keyed by argv
    tuple.  No live ``docker.exe`` / ``podman.exe`` / ``wsl.exe`` is
    invoked.
"""

import json

from dataclasses import dataclass, field
from typing import Callable, Optional

from ccai import testbase

from ccai.interfaces.icommandrunner import RunResult

from ccai.sysman.windows.containermanagement import (
    WindowsContainerActionStatus,
    WindowsContainerEngine,
    WindowsContainerManager,
    WindowsContainerManagerConfig,
    WindowsContainerSpec,
    _build_create_argv,
    _parse_container_ps_lines,
    _parse_image_lines,
    _parse_inspect_first,
)


@dataclass
class _MockCommandRunner:
    """
        Minimal ``ICommandRunner`` stand-in.
    """

    results: dict = field(default_factory=dict)
    default_result: Optional[RunResult] = None
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
        call_record = {
            "args": list(args),
            "elevated": elevated,
            "stdin": stdin,
        }
        self.calls.append(call_record)

        key = tuple(args)
        if key in self.results:
            rtnval = self.results[key]
            return rtnval

        if self.default_result is not None:
            rtnval = self.default_result
            return rtnval

        rtnval = RunResult(exit_code=0, stdout="", stderr="")
        return rtnval


_DOCKER_EXE = "docker.exe"


def _docker_images_filter_argv(*, ref: str) -> tuple:
    rtnval = (
        _DOCKER_EXE, "images",
        "--filter", "reference={}".format(ref),
        "--format", "{{json .}}",
    )
    return rtnval


def _docker_inspect_argv(*, target: str) -> tuple:
    rtnval = (_DOCKER_EXE, "inspect", target)
    return rtnval


def _seed_image_present(
    *,
    runner: _MockCommandRunner,
    ref: str,
    present: bool,
) -> None:
    key = _docker_images_filter_argv(ref=ref)
    if present:
        payload = json.dumps({"ID": "sha256:abc", "Repository": "x"}) + "\n"
    else:
        payload = ""
    runner.results[key] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    return


def _seed_inspect_absent(
    *,
    runner: _MockCommandRunner,
    target: str,
) -> None:
    key = _docker_inspect_argv(target=target)
    runner.results[key] = RunResult(
        exit_code=1, stdout="", stderr="No such object",
    )
    return


def _seed_inspect_record(
    *,
    runner: _MockCommandRunner,
    target: str,
    record: dict,
) -> None:
    key = _docker_inspect_argv(target=target)
    payload = json.dumps([record])
    runner.results[key] = RunResult(
        exit_code=0, stdout=payload, stderr="",
    )
    return


# ----------------------------------------------------------------------
# pull_image
# ----------------------------------------------------------------------


def test_pull_image_invokes_engine_pull():
    """
        pull_image dispatches ``docker.exe pull <ref>``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_image_present(runner=runner, ref="nginx:1.25", present=False)
    manager = WindowsContainerManager(
        runner=runner,
        config=WindowsContainerManagerConfig(),
    )
    result = manager.pull_image(ref="nginx:1.25")

    testbase.assert_equal(result.status, WindowsContainerActionStatus.SUCCESS)
    pull_calls = [
        c for c in runner.calls if c["args"][:2] == ["docker.exe", "pull"]
    ]
    testbase.assert_equal(len(pull_calls), 1)
    testbase.assert_equal(
        pull_calls[0]["args"], ["docker.exe", "pull", "nginx:1.25"],
    )
    testbase.assert_equal(result.engine, WindowsContainerEngine.DOCKER)
    return


def test_pull_image_skipped_when_already_present():
    """
        pull_image returns SKIPPED when the image is already present.
    """
    runner = _MockCommandRunner()
    _seed_image_present(runner=runner, ref="nginx:1.25", present=True)
    manager = WindowsContainerManager(
        runner=runner,
        config=WindowsContainerManagerConfig(),
    )
    result = manager.pull_image(ref="nginx:1.25")

    testbase.assert_equal(result.status, WindowsContainerActionStatus.SKIPPED)
    pull_calls = [
        c for c in runner.calls if c["args"][:2] == ["docker.exe", "pull"]
    ]
    testbase.assert_equal(pull_calls, [])
    return


def test_pull_image_dry_run_short_circuits():
    """
        Dry-run records the would-have-been argv without invoking the
        runner.
    """
    runner = _MockCommandRunner()
    manager = WindowsContainerManager(
        runner=runner,
        config=WindowsContainerManagerConfig(dry_run=True),
    )
    result = manager.pull_image(ref="nginx:1.25")

    testbase.assert_equal(result.status, WindowsContainerActionStatus.DRY_RUN)
    testbase.assert_equal(len(runner.calls), 0)
    testbase.assert_equal(
        result.command, ["docker.exe", "pull", "nginx:1.25"],
    )
    return


# ----------------------------------------------------------------------
# remove_image
# ----------------------------------------------------------------------


def test_remove_image_invokes_rmi():
    """
        remove_image dispatches ``docker.exe rmi <ref>``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_image_present(runner=runner, ref="nginx:1.25", present=True)
    manager = WindowsContainerManager(
        runner=runner,
        config=WindowsContainerManagerConfig(),
    )
    result = manager.remove_image(ref="nginx:1.25")

    testbase.assert_equal(result.status, WindowsContainerActionStatus.SUCCESS)
    rmi_calls = [
        c for c in runner.calls if c["args"][:2] == ["docker.exe", "rmi"]
    ]
    testbase.assert_equal(len(rmi_calls), 1)
    testbase.assert_equal(
        rmi_calls[0]["args"], ["docker.exe", "rmi", "nginx:1.25"],
    )
    return


# ----------------------------------------------------------------------
# list_images
# ----------------------------------------------------------------------


def test_list_images_parses_json_lines():
    """
        list_images parses one JSON object per line.
    """
    runner = _MockCommandRunner()
    payload_lines = [
        json.dumps({
            "ID": "sha256:abc",
            "Repository": "nginx",
            "Tag": "1.25",
            "Digest": "sha256:def",
            "Size": 12345678,
            "CreatedAt": "2025-01-01T00:00:00Z",
        }),
    ]
    runner.results[
        ("docker.exe", "images", "--format", "{{json .}}")
    ] = RunResult(
        exit_code=0, stdout="\n".join(payload_lines), stderr="",
    )
    manager = WindowsContainerManager(
        runner=runner,
        config=WindowsContainerManagerConfig(),
    )
    images = manager.list_images()

    testbase.assert_equal(len(images), 1)
    testbase.assert_equal(images[0].repository, "nginx")
    return


# ----------------------------------------------------------------------
# create_container
# ----------------------------------------------------------------------


def test_create_container_invokes_create_with_spec():
    """
        create_container dispatches ``docker.exe create --name <name>
        ... <image> [argv...]`` with a Windows-style volume mount.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(
            exit_code=0, stdout="containerabc\n", stderr="",
        ),
    )
    _seed_inspect_absent(runner=runner, target="web")
    spec = WindowsContainerSpec(
        name="web",
        image="nginx:1.25",
        volumes=["C:\\srv:/data"],
        ports=["8080:80"],
        env={"FOO": "bar"},
        restart_policy="unless-stopped",
    )
    manager = WindowsContainerManager(
        runner=runner,
        config=WindowsContainerManagerConfig(),
    )
    result = manager.create_container(spec=spec)

    testbase.assert_equal(result.status, WindowsContainerActionStatus.SUCCESS)
    create_calls = [
        c for c in runner.calls if c["args"][:2] == ["docker.exe", "create"]
    ]
    testbase.assert_equal(len(create_calls), 1)
    argv = create_calls[0]["args"]
    testbase.assert_true("--name" in argv)
    testbase.assert_true("web" in argv)
    testbase.assert_true("--restart=unless-stopped" in argv)
    testbase.assert_true("FOO=bar" in argv)
    testbase.assert_true("C:\\srv:/data" in argv)
    testbase.assert_true("8080:80" in argv)
    testbase.assert_true("nginx:1.25" in argv)
    testbase.assert_equal(result.container_id, "containerabc")
    return


def test_create_container_skipped_when_name_present_with_same_spec():
    """
        create_container returns SKIPPED when a container of the same
        name already exists with the same image.
    """
    runner = _MockCommandRunner()
    _seed_inspect_record(
        runner=runner, target="web",
        record={
            "Id": "abc",
            "Name": "/web",
            "Config": {"Image": "nginx:1.25"},
            "State": {"Running": False, "Status": "created"},
        },
    )
    spec = WindowsContainerSpec(name="web", image="nginx:1.25")
    manager = WindowsContainerManager(
        runner=runner,
        config=WindowsContainerManagerConfig(),
    )
    result = manager.create_container(spec=spec)

    testbase.assert_equal(result.status, WindowsContainerActionStatus.SKIPPED)
    create_calls = [
        c for c in runner.calls if c["args"][:2] == ["docker.exe", "create"]
    ]
    testbase.assert_equal(create_calls, [])
    return


# ----------------------------------------------------------------------
# start_container / stop_container / remove_container
# ----------------------------------------------------------------------


def test_start_container_skipped_when_running():
    """
        start_container returns SKIPPED when State.Running is True.
    """
    runner = _MockCommandRunner()
    _seed_inspect_record(
        runner=runner, target="web",
        record={
            "Id": "abc",
            "Name": "/web",
            "Config": {"Image": "nginx:1.25"},
            "State": {"Running": True, "Status": "running"},
        },
    )
    manager = WindowsContainerManager(
        runner=runner,
        config=WindowsContainerManagerConfig(),
    )
    result = manager.start_container(container_id="web")

    testbase.assert_equal(result.status, WindowsContainerActionStatus.SKIPPED)
    return


def test_stop_container_skipped_when_stopped():
    """
        stop_container returns SKIPPED when the container is not
        running.
    """
    runner = _MockCommandRunner()
    _seed_inspect_record(
        runner=runner, target="web",
        record={
            "Id": "abc",
            "Name": "/web",
            "Config": {"Image": "nginx:1.25"},
            "State": {"Running": False, "Status": "exited"},
        },
    )
    manager = WindowsContainerManager(
        runner=runner,
        config=WindowsContainerManagerConfig(),
    )
    result = manager.stop_container(container_id="web")

    testbase.assert_equal(result.status, WindowsContainerActionStatus.SKIPPED)
    stop_calls = [
        c for c in runner.calls if c["args"][:2] == ["docker.exe", "stop"]
    ]
    testbase.assert_equal(stop_calls, [])
    return


def test_remove_container_invokes_rm_force():
    """
        remove_container dispatches ``docker.exe rm -f <id>``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_inspect_record(
        runner=runner, target="web",
        record={
            "Id": "abc",
            "Name": "/web",
            "Config": {"Image": "nginx:1.25"},
            "State": {"Running": True, "Status": "running"},
        },
    )
    manager = WindowsContainerManager(
        runner=runner,
        config=WindowsContainerManagerConfig(),
    )
    result = manager.remove_container(container_id="web")

    testbase.assert_equal(result.status, WindowsContainerActionStatus.SUCCESS)
    rm_calls = [
        c for c in runner.calls if c["args"][:2] == ["docker.exe", "rm"]
    ]
    testbase.assert_equal(len(rm_calls), 1)
    testbase.assert_equal(
        rm_calls[0]["args"], ["docker.exe", "rm", "-f", "web"],
    )
    return


# ----------------------------------------------------------------------
# list_containers / inspect_container
# ----------------------------------------------------------------------


def test_list_containers_with_all_includes_stopped():
    """
        list_containers(all=True) adds ``-a``.
    """
    runner = _MockCommandRunner()
    line = json.dumps({
        "ID": "abc",
        "Names": ["web"],
        "Image": "nginx:1.25",
        "Status": "Exited (0)",
        "State": "exited",
        "CreatedAt": "2025-01-01T00:00:00Z",
        "Ports": "0.0.0.0:8080->80/tcp",
        "Mounts": "",
        "Command": "nginx",
    })
    runner.results[
        ("docker.exe", "ps", "-a", "--format", "{{json .}}")
    ] = RunResult(exit_code=0, stdout=line, stderr="")
    manager = WindowsContainerManager(
        runner=runner,
        config=WindowsContainerManagerConfig(),
    )
    containers = manager.list_containers(all=True)

    testbase.assert_equal(len(containers), 1)
    testbase.assert_equal(containers[0].state, "exited")
    return


def test_inspect_container_parses_json_array():
    """
        inspect_container parses ``docker.exe inspect`` (JSON array).
    """
    runner = _MockCommandRunner()
    _seed_inspect_record(
        runner=runner, target="web",
        record={
            "Id": "abc",
            "Name": "/web",
            "Config": {
                "Image": "nginx:1.25",
                "Cmd": ["nginx"],
            },
            "State": {
                "Running": True,
                "Status": "running",
                "StartedAt": "2025-02-01T12:00:00Z",
            },
            "Created": "2025-02-01T11:55:00Z",
            "NetworkSettings": {"Ports": {"80/tcp": []}},
            "Mounts": [{"Destination": "/data"}],
        },
    )
    manager = WindowsContainerManager(
        runner=runner,
        config=WindowsContainerManagerConfig(),
    )
    info = manager.inspect_container(container_id="web")

    testbase.assert_true(info is not None)
    testbase.assert_equal(info.name, "web")
    testbase.assert_equal(info.image, "nginx:1.25")
    testbase.assert_equal(info.started, "2025-02-01T12:00:00Z")
    return


# ----------------------------------------------------------------------
# get_container_logs / exec_in_container
# ----------------------------------------------------------------------


def test_get_container_logs_invokes_logs_with_tail():
    """
        get_container_logs dispatches
        ``docker.exe logs --tail <n> <id>``.
    """
    runner = _MockCommandRunner()
    runner.results[
        ("docker.exe", "logs", "--tail", "50", "web")
    ] = RunResult(
        exit_code=0,
        stdout="line1\nline2\n",
        stderr="",
    )
    manager = WindowsContainerManager(
        runner=runner,
        config=WindowsContainerManagerConfig(),
    )
    logs = manager.get_container_logs(container_id="web", lines=50)

    testbase.assert_equal(logs.lines, ["line1", "line2"])
    testbase.assert_equal(logs.truncated, True)
    return


def test_exec_in_container_invokes_exec():
    """
        exec_in_container dispatches
        ``docker.exe exec <id> <argv...>``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(
            exit_code=0, stdout="hello\n", stderr="",
        ),
    )
    manager = WindowsContainerManager(
        runner=runner,
        config=WindowsContainerManagerConfig(),
    )
    result = manager.exec_in_container(
        container_id="web", argv=["echo", "hello"],
    )

    testbase.assert_equal(result.status, WindowsContainerActionStatus.SUCCESS)
    exec_calls = [
        c for c in runner.calls if c["args"][:2] == ["docker.exe", "exec"]
    ]
    testbase.assert_equal(len(exec_calls), 1)
    testbase.assert_equal(
        exec_calls[0]["args"],
        ["docker.exe", "exec", "web", "echo", "hello"],
    )
    return


# ----------------------------------------------------------------------
# Engine selection — Podman via WSL
# ----------------------------------------------------------------------


def test_engine_selection_dispatches_to_podman_via_wsl_when_configured():
    """
        Configuring engine=PODMAN_WSL with use_wsl_for_podman=True
        prefixes every verb with ``wsl.exe podman``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "wsl.exe", "podman", "images",
        "--filter", "reference=alpine:3", "--format", "{{json .}}",
    )] = RunResult(exit_code=0, stdout="", stderr="")
    manager = WindowsContainerManager(
        runner=runner,
        config=WindowsContainerManagerConfig(
            engine=WindowsContainerEngine.PODMAN_WSL,
            use_wsl_for_podman=True,
        ),
    )
    result = manager.pull_image(ref="alpine:3")

    testbase.assert_equal(result.status, WindowsContainerActionStatus.SUCCESS)
    pull_calls = [
        c for c in runner.calls
        if c["args"][:3] == ["wsl.exe", "podman", "pull"]
    ]
    testbase.assert_equal(len(pull_calls), 1)
    testbase.assert_equal(result.engine, WindowsContainerEngine.PODMAN_WSL)
    return


def test_engine_selection_uses_native_podman_exe_when_use_wsl_false():
    """
        With ``use_wsl_for_podman=False`` the manager invokes
        ``podman.exe`` directly rather than going through ``wsl``.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    runner.results[(
        "podman.exe", "images",
        "--filter", "reference=alpine:3", "--format", "{{json .}}",
    )] = RunResult(exit_code=0, stdout="", stderr="")
    manager = WindowsContainerManager(
        runner=runner,
        config=WindowsContainerManagerConfig(
            engine=WindowsContainerEngine.PODMAN_WSL,
            use_wsl_for_podman=False,
        ),
    )
    result = manager.pull_image(ref="alpine:3")

    testbase.assert_equal(result.status, WindowsContainerActionStatus.SUCCESS)
    pull_calls = [
        c for c in runner.calls if c["args"][:2] == ["podman.exe", "pull"]
    ]
    testbase.assert_equal(len(pull_calls), 1)
    return


# ----------------------------------------------------------------------
# Elevation
# ----------------------------------------------------------------------


def test_writes_pass_elevated_per_config():
    """
        Every write verb passes ``elevated=True`` when the config
        requests elevation.
    """
    runner = _MockCommandRunner(
        default_result=RunResult(exit_code=0, stdout="", stderr=""),
    )
    _seed_image_present(runner=runner, ref="nginx:1.25", present=False)
    manager = WindowsContainerManager(
        runner=runner,
        config=WindowsContainerManagerConfig(elevated=True),
    )
    manager.pull_image(ref="nginx:1.25")

    pull_calls = [
        c for c in runner.calls if c["args"][:2] == ["docker.exe", "pull"]
    ]
    testbase.assert_equal(len(pull_calls), 1)
    testbase.assert_equal(pull_calls[0]["elevated"], True)
    return


def test_reads_never_elevate():
    """
        Read verbs never pass ``elevated=True`` regardless of config.
    """
    runner = _MockCommandRunner()
    runner.results[
        ("docker.exe", "images", "--format", "{{json .}}")
    ] = RunResult(exit_code=0, stdout="", stderr="")
    runner.results[
        ("docker.exe", "ps", "--format", "{{json .}}")
    ] = RunResult(exit_code=0, stdout="", stderr="")
    manager = WindowsContainerManager(
        runner=runner,
        config=WindowsContainerManagerConfig(elevated=True),
    )
    manager.list_images()
    manager.list_containers()

    for call in runner.calls:
        testbase.assert_equal(call["elevated"], False)
    return


# ----------------------------------------------------------------------
# Error surface
# ----------------------------------------------------------------------


def test_failed_command_surfaces_stderr():
    """
        FAILED writes carry the runner's exit code, stdout, and stderr.
    """
    runner = _MockCommandRunner()
    _seed_image_present(runner=runner, ref="nginx:1.25", present=False)
    runner.default_result = RunResult(
        exit_code=125, stdout="", stderr="error during connect",
    )
    manager = WindowsContainerManager(
        runner=runner,
        config=WindowsContainerManagerConfig(),
    )
    result = manager.pull_image(ref="nginx:1.25")

    testbase.assert_equal(result.status, WindowsContainerActionStatus.FAILED)
    testbase.assert_equal(result.exit_code, 125)
    testbase.assert_equal(result.stderr, "error during connect")
    return


# ----------------------------------------------------------------------
# Module-level parser tests
# ----------------------------------------------------------------------


def test_parse_image_lines_handles_multi_line_payload():
    payload = "\n".join([
        json.dumps({"ID": "1", "Repository": "a", "Tag": "1"}),
        json.dumps({"ID": "2", "Repository": "b", "Tag": "2"}),
    ])
    images = _parse_image_lines(output=payload)
    testbase.assert_equal(len(images), 2)
    testbase.assert_equal(images[0].id, "1")
    return


def test_parse_inspect_first_returns_element_zero():
    payload = json.dumps([{"Id": "first"}, {"Id": "second"}])
    rtnval = _parse_inspect_first(output=payload)
    testbase.assert_true(rtnval is not None)
    testbase.assert_equal(rtnval.get("Id"), "first")
    return


def test_parse_container_ps_lines_handles_string_ports():
    payload = json.dumps({
        "ID": "abc",
        "Names": ["web"],
        "Image": "nginx",
        "Status": "Up",
        "State": "running",
        "Ports": "0.0.0.0:80->80/tcp, 0.0.0.0:443->443/tcp",
    })
    rows = _parse_container_ps_lines(
        output=payload, engine=WindowsContainerEngine.DOCKER,
    )
    testbase.assert_equal(len(rows), 1)
    testbase.assert_equal(len(rows[0].ports), 2)
    return


def test_build_create_argv_orders_flags_consistently():
    spec = WindowsContainerSpec(
        name="db",
        image="postgres:16",
        argv=["postgres"],
        env={"POSTGRES_PASSWORD": "x"},
    )
    argv = _build_create_argv(prefix=["docker.exe"], spec=spec)
    testbase.assert_equal(argv[0], "docker.exe")
    testbase.assert_equal(argv[1], "create")
    testbase.assert_equal(argv[-2], "postgres:16")
    testbase.assert_equal(argv[-1], "postgres")
    return

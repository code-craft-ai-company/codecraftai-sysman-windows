# Design

## Concept Mapping (Windows ↔ Ubuntu / RHEL ↔ macOS)

The cross-OS container-management contract talks about *images* and
*containers*.  Windows hosts the engine in two modes:

* **Docker Desktop** ships `docker.exe` on PATH and runs containers
  inside a WSL2 distro (Linux containers — the modern default) or
  via the native Windows containerd backend (Windows containers).
  This manager invokes `docker.exe` directly and is agnostic about
  which backend Docker Desktop is configured for; the binary is the
  same in both cases.
* **Podman on Windows** has a native Windows port (`podman.exe`) that
  drives a WSL2-hosted machine via `podman machine`.  The cross-OS
  contract enumerates this as a separate engine; this manager
  surfaces it as `WindowsContainerEngine.PODMAN_WSL`.  In production
  the indirection is more commonly via `wsl.exe podman <argv>` —
  invoking podman inside an existing WSL2 distro — than via the
  Windows port; the manager exposes a config flag
  (`use_wsl_for_podman`) that selects.

| Contract concept | Windows (this spec) | Ubuntu | macOS |
|---|---|---|---|
| Engine | `docker.exe` (Docker Desktop) or `podman.exe` / `wsl.exe podman` | `docker` / `podman` | `docker` / `podman` |
| Engine implementation | WSL2 (Linux containers) or Windows containerd (Windows containers) | Native Linux daemon | Linux VM via Docker Desktop / colima / `podman machine` |
| Image storage | Inside the WSL2 distro (Linux mode) or in a Windows ContentTrust store (Windows mode) | `/var/lib/docker` or `~/.local/share/containers` | Inside the VM disk |
| Volume mount syntax (Linux containers) | ``-v 'C:\\host\\path:/container/path'`` — Windows path on left, Linux path on right; Docker Desktop's WSL2 backend translates the host portion | POSIX paths | POSIX paths |
| Volume mount syntax (Windows containers) | ``-v 'C:\\host:C:\\container'`` — Windows path on both sides | n/a | n/a |
| Port mapping syntax | `<host>:<container>[/proto]` | identical | identical |
| Network selection | `--network <name>` | identical | identical |
| Restart policy | `--restart=<policy>` | identical | identical |
| Elevation | None for Docker Desktop (runs as desktop user); the `elevated` flag is for rootful socket configurations or admin-token Windows Containers | `sudo` for rootful Docker; rootless Podman needs no sudo | None — Docker Desktop runs as the desktop user |

The Windows implementation reuses the same argv shape as Ubuntu /
macOS (the verbs and flags Docker / Podman accept are
cross-platform), but argv[0] becomes `docker.exe` / `podman.exe` /
`wsl.exe podman` and the design carries an extra section on volume
mount quoting because Windows paths embed colons that look like the
Docker volume separator at first glance.

---

## Overview

The container-management feature is implemented as a single module,
`automojo.sysman.windows.containermanagement`, that exposes a
`WindowsContainerManager` class.  The coordinator instantiates
`WindowsContainerManager` and calls its methods to perform discrete
container engine actions.

This feature **complements — and does not duplicate** — Docker
Desktop installation (`package-management`), WSL2 distro lifecycle
(per-OS extension), or Windows-feature enablement
(`configuration-management`).  Everything outside the
`WindowsContainerManager` verb set lives in another feature.

All command execution is delegated to a command runner implementing
the `ICommandRunner` interface.  The `WindowsContainerManager` never
directly calls `subprocess`, shell commands, or `open()`.

The feature works identically whether commands execute locally (via
`LocalCommandRunner`) or remotely over SSH / WinRM (via
`SSHCommandRunner`).

**See Also:** [Command Runner Architecture](../command-runner-architecture/design.md)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Maintenance Coordinator                        │
│  - Instantiates runner (Local or SSH/WinRM)                 │
│  - Configures aspects (optional)                            │
│  - Selects engine (DOCKER or PODMAN_WSL)                    │
│  - Injects runner into WindowsContainerManager              │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates & injects
                         ▼
┌─────────────────────────────────────────────────────────────┐
│            WindowsContainerManager                          │
│  automojo.sysman.windows.containermanagement                │
│                                                             │
│  pull_image()           remove_image()                      │
│  list_images()                                              │
│  create_container()     start_container()                   │
│  stop_container()       remove_container()                  │
│  list_containers()      inspect_container()                 │
│  get_container_logs()   exec_in_container()                 │
└─────────┬───────────────────────────────────────────────────┘
          │ ICommandRunner
          ▼
┌─────────────────────────────────────────────────────────────┐
│        LocalCommandRunner / SSHCommandRunner (WinRM)        │
└─────────┬───────────────────────────────────────────────────┘
          │ optional aspect composition
          ▼
┌─────────────────────────────────────────────────────────────┐
│   PersistenceAspect  RetryAspect  TimeoutAspect             │
└─────────────────────────────────────────────────────────────┘
```

---

## Components

### `WindowsContainerManager`

**Location:**
`source/packages/automojo/sysman/windows/containermanagement.py`

| Method | Signature | Description |
|---|---|---|
| `pull_image` | `(ref) → WindowsContainerActionResult` | `<engine_prefix> pull <ref>`; idempotent via `images --filter` |
| `remove_image` | `(ref) → WindowsContainerActionResult` | `<engine_prefix> rmi <ref>`; idempotent via `images --filter` |
| `list_images` | `() → list[WindowsImageInfo]` | `<engine_prefix> images --format '{{json .}}'` |
| `create_container` | `(spec) → WindowsContainerActionResult` | `<engine_prefix> create --name <name> [<flags...>] <image> [<argv...>]` |
| `start_container` | `(container_id) → WindowsContainerActionResult` | `<engine_prefix> start <id>` |
| `stop_container` | `(container_id) → WindowsContainerActionResult` | `<engine_prefix> stop -t <timeout> <id>` |
| `remove_container` | `(container_id) → WindowsContainerActionResult` | `<engine_prefix> rm -f <id>` |
| `list_containers` | `(all=False) → list[WindowsContainerInfo]` | `<engine_prefix> ps [-a] --format '{{json .}}'` |
| `inspect_container` | `(container_id) → WindowsContainerInfo \| None` | `<engine_prefix> inspect <id>` |
| `get_container_logs` | `(container_id, lines=100) → WindowsContainerLogs` | `<engine_prefix> logs --tail <lines> <id>` |
| `exec_in_container` | `(container_id, argv) → WindowsContainerExecResult` | `<engine_prefix> exec <id> <argv...>` |

### `WindowsContainerManagerConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | `bool` | `False` | When `True`, no command is dispatched |
| `elevated` | `bool` | `False` | When `True`, writes pass `elevated=True` |
| `engine` | `WindowsContainerEngine` | `DOCKER` | Engine to invoke |
| `force_pull` | `bool` | `False` | When `True`, `pull_image` always invokes `pull` |
| `default_stop_timeout` | `int` | `10` | Default `-t` value passed to `<engine> stop` |
| `use_wsl_for_podman` | `bool` | `True` | When `True` and engine is `PODMAN_WSL`, prefixes every podman invocation with `wsl.exe podman` instead of `podman.exe` |
| `docker_executable` | `str` | `"docker.exe"` | Docker binary token (PATH-resolved) |

---

## Data Models

### Result Types

```python
class WindowsContainerActionStatus(Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"
    DRY_RUN  = "dry_run"

class WindowsContainerEngine(Enum):
    DOCKER = "docker"
    PODMAN_WSL = "podman_wsl"

@dataclass
class WindowsContainerActionResult:
    status: WindowsContainerActionStatus
    detail: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
    engine: WindowsContainerEngine | None
    container_id: str | None
    image: str | None
    target: str | None
    message: str | None

@dataclass
class WindowsContainerExecResult:
    status: WindowsContainerActionStatus
    detail: str | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    command: list[str] | None
    engine: WindowsContainerEngine | None
    container_id: str | None
    command_argv: list[str] | None
    output: str | None
```

### Image / Container Types

```python
@dataclass
class WindowsImageInfo:
    id: str
    repository: str | None
    tag: str | None
    digest: str | None
    size_bytes: int | None
    created: str

@dataclass
class WindowsContainerSpec:
    name: str
    image: str
    argv: list[str] | None = None
    env: dict[str, str] | None = None
    volumes: list[str] | None = None      # "C:\\host:/data" or "C:\\host:C:\\container"
    ports: list[str] | None = None
    networks: list[str] | None = None
    restart_policy: str | None = None
    working_dir: str | None = None
    user: str | None = None
    detach: bool = True

@dataclass
class WindowsContainerInfo:
    id: str
    name: str
    image: str
    status: str
    state: str
    created: str
    started: str | None
    ports: list[str]
    mounts: list[str]
    command: list[str]
    engine: WindowsContainerEngine | None

@dataclass
class WindowsContainerLogs:
    container_id: str
    lines: list[str]
    truncated: bool
```

### Exceptions

```python
class WindowsContainerQueryError(Exception):
    """Raised when an inspect / list query fails unexpectedly."""

class WindowsContainerEngineNotFoundError(Exception):
    """Raised when the configured engine binary is not on PATH."""

class WindowsContainerNotFoundError(Exception):
    """Reserved for cross-OS-contract parity."""
```

---

## Engine Mapping

| `WindowsContainerEngine` | argv prefix | Notes |
|---|---|---|
| `DOCKER` | `[docker_executable]` | Default.  Docker Desktop installs `docker.exe` on PATH.  The same binary works for Linux containers (WSL2 backend) and Windows containers (containerd backend). |
| `PODMAN_WSL` (use_wsl=True) | `["wsl.exe", "podman"]` | Default for Podman.  `wsl.exe` invokes the default WSL2 distro; `podman` is the in-distro binary.  This is the common production path because the native Windows port of Podman is rare. |
| `PODMAN_WSL` (use_wsl=False) | `["podman.exe"]` | The native Windows port (`brew install podman` equivalent for Windows; installed via MSI).  Drives a managed WSL2 distro internally via `podman machine`. |

The cross-OS contract enumerates `CONTAINERD` for Windows-Container
environments.  This implementation covers Windows Containers via the
`DOCKER` engine path because Docker Desktop installs the same
`docker.exe` regardless of mode; `nerdctl` against a raw containerd
socket on Windows is rare and is reserved for a future engine
identifier (see follow-ups in `tasks.md`).

### Why `wsl.exe podman` is the default for `PODMAN_WSL`

Podman has a native Windows binary, but in practice most production
Windows hosts that run podman do so by installing it inside a WSL2
Linux distro and invoking it via `wsl.exe podman`.  This keeps the
container runtime in the Linux user-space stack where podman is
mature, while the Windows side never runs podman code at all.  The
manager's default reflects that.

### Volume Mount Quoting

| Backend | Volume token format | Example |
|---|---|---|
| Docker Desktop, Linux containers (WSL2) | `<windows-path>:<linux-path>[:opts]` | `C:\\srv:/data:ro` |
| Docker Desktop, Windows containers | `<windows-path>:<windows-path>[:opts]` | `C:\\srv:C:\\srv:ro` |
| Podman via WSL2 | `<windows-path>:<linux-path>[:opts]` (Docker-Desktop-equivalent — host path is interpreted by WSL2) | `C:\\srv:/data` |

Coordinators MUST format the host portion of the volume token as a
**Windows path with double-backslash escapes** when the path is
embedded in Python source (so the string survives Python's own
escape handling); the on-the-wire token is single-backslash
(`C:\srv:/data`).  Tests in `test_containermanagement.py` use the
double-backslash Python literal to exercise the round-trip.

The runner passes argv directly to the engine (no shell expansion),
so spaces in the host path are preserved without extra quoting and
the `:` separator is not consumed by the shell.  The manager does
not rewrite paths — coordinators that need cross-platform path
construction should resolve before calling.

---

## Verb / Command Mappings

### Write Commands (elevation honoured)

| Operation | Argv |
|---|---|
| `pull_image` | `<engine_prefix> pull <ref>` |
| `remove_image` | `<engine_prefix> rmi <ref>` |
| `create_container` | `<engine_prefix> create --name <name> [--restart=<policy>] [--user <u>] [--workdir <d>] [-e <kv> ...] [-v <vol> ...] [-p <port> ...] [--network <n> ...] <image> [<argv...>]` |
| `start_container` | `<engine_prefix> start <id>` |
| `stop_container` | `<engine_prefix> stop -t <timeout> <id>` |
| `remove_container` | `<engine_prefix> rm -f <id>` |
| `exec_in_container` | `<engine_prefix> exec <id> <argv...>` |

### Read Commands (no elevation)

| Operation | Argv |
|---|---|
| `list_images` | `<engine_prefix> images --format '{{json .}}'` |
| `list_containers` | `<engine_prefix> ps [-a] --format '{{json .}}'` |
| `inspect_container` | `<engine_prefix> inspect <id>` |
| `get_container_logs` | `<engine_prefix> logs --tail <lines> <id>` |
| `image_present` probe | `<engine_prefix> images --filter 'reference=<ref>' --format '{{json .}}'` |
| `inspect_raw` probe | `<engine_prefix> inspect <id>` |

### Argv Construction Order for `create_container`

Same as Ubuntu / macOS — the `_build_create_argv` helper enforces:

1. `<engine_prefix> create --name <name>`
2. `--restart=<policy>` (when set)
3. `--user <user>` (when set)
4. `--workdir <dir>` (when set)
5. `-e KEY=VALUE` (one per env entry)
6. `-v <volume_token>` (one per volume)
7. `-p <port_token>` (one per port)
8. `--network <name>` (one per network)
9. `<image>`
10. `<argv...>`

---

## Output Parsing

### `<engine_prefix> images --format '{{json .}}'`

```text
{"ID":"sha256:abc","Repository":"nginx","Tag":"1.25","Digest":"sha256:def","Size":12345678,"CreatedAt":"2025-01-01T00:00:00Z"}
```

* One JSON object per line.
* `<none>` repository / tag / digest values are coerced to `None`.
* `Size` is reported as int (Podman) or string (Docker) — both
  tolerated.

### `<engine_prefix> ps --format '{{json .}}'`

* `Names` may be a list or string.
* `Ports` and `Mounts` may be a list or comma-separated string.
* `Command` may be a list or string.

### `<engine_prefix> inspect <id>`

* The response is a JSON ARRAY; parser picks element 0.
* `Name` carries a leading `/`; the parser strips it.
* `Mounts[*].Destination` is collected into the `mounts` list.

---

## Idempotency Guards

| Method | Guard | Source |
|---|---|---|
| `pull_image` | Reference already locally present | `<engine_prefix> images --filter 'reference=<ref>'` |
| `remove_image` | Reference not present | `<engine_prefix> images --filter 'reference=<ref>'` |
| `create_container` | Container of same name + image exists | `<engine_prefix> inspect <name>` |
| `start_container` | `State.Running == True` | `<engine_prefix> inspect <id>` |
| `stop_container` | `State.Running == False` (or absent) | `<engine_prefix> inspect <id>` |
| `remove_container` | Container absent | `<engine_prefix> inspect <id>` |
| `exec_in_container` | None — operational, always runs | n/a |

---

## Error Handling

### Typed Exceptions

| Exception | Raised When |
|---|---|
| `WindowsContainerQueryError` | Reserved for stricter read paths. |
| `WindowsContainerEngineNotFoundError` | Reserved for engine-availability probe. |
| `WindowsContainerNotFoundError` | Reserved for cross-OS-contract parity. |

All write failures surface as
`WindowsContainerActionResult(status=FAILED, ...)` carrying the
runner's exit code, stdout, and stderr.

---

## Correctness Properties

### Property 1: pull_image Argv (Docker)

*For any* `pull_image(ref=R)` with `engine=DOCKER`, the runner sees
`["docker.exe", "pull", R]`.

**Validates: Requirements 1.1, 12.1**

### Property 2: pull_image Argv (Podman via WSL)

*For any* `pull_image(ref=R)` with `engine=PODMAN_WSL` and
`use_wsl_for_podman=True`, the runner sees
`["wsl.exe", "podman", "pull", R]`.

**Validates: Requirements 1.1, 12.2**

### Property 3: pull_image Argv (Native Podman)

*For any* `pull_image(ref=R)` with `engine=PODMAN_WSL` and
`use_wsl_for_podman=False`, the runner sees
`["podman.exe", "pull", R]`.

**Validates: Requirements 1.1, 12.3**

### Property 4: pull_image Idempotency

*For any* `pull_image(ref=R)` where `R` is present and
`force_pull=False`, the manager MUST return SKIPPED.

**Validates: Requirements 1.2**

### Property 5: remove_image Argv

*For any* `remove_image(ref=R)`, the runner sees
`["docker.exe", "rmi", R]` (or the prefix-equivalent for podman).

**Validates: Requirements 2.1**

### Property 6: list_images Parses JSON Lines

*For any* engine output of N JSON objects (one per line),
`list_images()` MUST return N rows.

**Validates: Requirements 3.2**

### Property 7: create_container Argv Skeleton

*For any* `WindowsContainerSpec(name=N, image=I)`,
`create_container()` MUST dispatch an argv that contains
`["create", "--name", N, ..., I]` after the engine prefix.

**Validates: Requirements 4.1**

### Property 8: create_container Volume Token Round-Trip

*For any* `spec.volumes=["C:\\host:/data"]`, the dispatched argv
MUST contain the literal token `C:\host:/data` (no path rewriting).

**Validates: Requirements 4.2**

### Property 9: create_container Idempotency

*For any* `create_container(spec)` where a container of `spec.name`
exists with `Config.Image == spec.image`, the manager MUST return
SKIPPED.

**Validates: Requirements 4.3**

### Property 10: start_container Idempotency

*For any* `start_container(id)` where `State.Running == True`, the
manager MUST return SKIPPED.

**Validates: Requirements 5.2**

### Property 11: stop_container Argv

*For any* `stop_container(id=I)`, the runner sees the argv
`<engine_prefix> stop -t <default_stop_timeout> <I>`.

**Validates: Requirements 6.1**

### Property 12: stop_container Idempotency

*For any* `stop_container(id)` where `State.Running == False` or
absent, the manager MUST return SKIPPED.

**Validates: Requirements 6.2, 6.3**

### Property 13: remove_container Argv

*For any* `remove_container(id=I)`, the runner sees the argv
`<engine_prefix> rm -f <I>`.

**Validates: Requirements 7.1**

### Property 14: list_containers Includes Stopped When all=True

*For any* `list_containers(all=True)`, the argv contains `"-a"`.

**Validates: Requirements 8.1, 8.2**

### Property 15: inspect_container Returns Element Zero

*For any* successful inspect, the manager returns an info derived
from element 0.

**Validates: Requirements 9.2**

### Property 16: inspect_container Returns None on Absence

*For any* inspect with non-zero exit, the manager returns `None`.

**Validates: Requirements 9.4**

### Property 17: get_container_logs Sets truncated

*For any* `get_container_logs(id, lines)`, `truncated` MUST be True.

**Validates: Requirements 10.2**

### Property 18: exec_in_container Never Skips

*For any* `exec_in_container` outside dry-run, the manager MUST
dispatch `<engine_prefix> exec`.

**Validates: Requirements 11.3**

### Property 19: Dry-Run Safety

*For any* write verb invoked with `dry_run=True`, the runner MUST
NOT be invoked and the result status MUST be `DRY_RUN`.

**Validates: Requirements 1.6, 2.5, 4.6, 5.4, 6.5, 7.4, 11.4**

### Property 20: Elevation for Writes

*For any* write verb with `elevated=True` in the config, every
runner invocation MUST receive `elevated=True`.

**Validates: Requirements 12.4**

### Property 21: No Elevation for Reads

*For any* read verb (and any idempotency probe), the runner MUST
NEVER be invoked with `elevated=True`.

**Validates: Requirements 12.5**

### Property 22: Error Surface

*For any* non-zero exit from a write command, the result MUST have
`status=FAILED` and carry the runner's exit code and stderr.

**Validates: Requirements 1.5, 2.4, 4.5**

---

## Module Layout

```
source/packages/automojo/sysman/windows/
├── __init__.py
├── featurebase.py
├── containermanagement.py    ← WindowsContainerManager + types
└── storagemanagement.py

source/testroots/testbase/automojo/tests/sysman/windows/
└── test_containermanagement.py
```

---

## Usage Examples

### Local Execution (Default — Docker Desktop)

```python
from automojo.sysman.windows.runners import (
    LocalCommandRunner,
    LocalRunnerConfig,
)
from automojo.sysman.windows.containermanagement import (
    WindowsContainerEngine,
    WindowsContainerManager,
    WindowsContainerManagerConfig,
    WindowsContainerSpec,
)

runner = LocalCommandRunner(LocalRunnerConfig())
config = WindowsContainerManagerConfig(
    engine=WindowsContainerEngine.DOCKER,
)
containers = WindowsContainerManager(runner=runner, config=config)

# Pull, create with a Windows host volume, start
containers.pull_image(ref="nginx:1.25")
spec = WindowsContainerSpec(
    name="web",
    image="nginx:1.25",
    ports=["8080:80"],
    volumes=["C:\\srv:/data"],
)
containers.create_container(spec=spec)
containers.start_container(container_id="web")

info = containers.inspect_container(container_id="web")
print(info.state)
```

### Switching to Podman via WSL2

```python
config = WindowsContainerManagerConfig(
    engine=WindowsContainerEngine.PODMAN_WSL,
    use_wsl_for_podman=True,
)
containers = WindowsContainerManager(runner=runner, config=config)
# Every verb is now prefixed with ["wsl.exe", "podman"].  The host
# must already have the default WSL2 distro running with podman
# installed; this manager does not provision the distro.
```

### Switching to Native Windows Podman

```python
config = WindowsContainerManagerConfig(
    engine=WindowsContainerEngine.PODMAN_WSL,
    use_wsl_for_podman=False,
)
containers = WindowsContainerManager(runner=runner, config=config)
# Every verb invokes podman.exe directly; the engine drives its own
# managed WSL2 machine via `podman machine`.
```

### Dry-Run Mode

```python
config = WindowsContainerManagerConfig(dry_run=True)
containers = WindowsContainerManager(runner=runner, config=config)
result = containers.remove_container(container_id="web")
assert result.status == WindowsContainerActionStatus.DRY_RUN
print(result.command)   # ["docker.exe", "rm", "-f", "web"]
```

### SSH/WinRM Execution with Aspects

```python
runner = SSHCommandRunner(SSHRunnerConfig(host="windows-host-1"))
runner.add_aspect(aspect=PersistenceAspect(...))
runner.add_aspect(aspect=RetryAspect(...))
containers = WindowsContainerManager(
    runner=runner,
    config=WindowsContainerManagerConfig(),
)
for c in containers.list_containers(all=True):
    print(c.id, c.name, c.state)
```

---

## Testing Strategy

* All tests use a `MockCommandRunner` keyed on argv tuples.
* Argv-construction tests assert the exact argv shape including the
  `docker.exe` suffix (or `wsl.exe podman` / `podman.exe` prefix
  for the Podman engine).
* Volume-token tests prove that `C:\host:/data` round-trips through
  the argv unchanged.
* Idempotency tests cover SKIPPED paths (image present / absent,
  container present / absent / running / stopped).
* Dry-run tests prove the runner is not invoked.
* Engine-selection tests prove that `PODMAN_WSL` with
  `use_wsl_for_podman=True` produces `["wsl.exe", "podman", ...]`
  and with `use_wsl_for_podman=False` produces
  `["podman.exe", ...]`.
* Elevation tests cover write/read elevation policy.
* Parser tests cover `images`, `ps`, and `inspect` JSON shapes.
* Failure surface is tested by seeding non-zero exits.
* No test requires a live engine, a running WSL2 distro, or a
  running Docker Desktop instance.

---

## Scope Mapping

| Contract verb | Windows implementation |
|---|---|
| `pull_image` | `<engine_prefix> pull <ref>` |
| `remove_image` | `<engine_prefix> rmi <ref>` |
| `list_images` | `<engine_prefix> images --format '{{json .}}'` |
| `create_container` | `<engine_prefix> create --name <name> [<flags...>] <image> [<argv...>]` |
| `start_container` | `<engine_prefix> start <id>` |
| `stop_container` | `<engine_prefix> stop -t <timeout> <id>` |
| `remove_container` | `<engine_prefix> rm -f <id>` |
| `list_containers` | `<engine_prefix> ps [-a] --format '{{json .}}'` |
| `inspect_container` | `<engine_prefix> inspect <id>` |
| `get_container_logs` | `<engine_prefix> logs --tail <lines> <id>` |
| `exec_in_container` | `<engine_prefix> exec <id> <argv...>` |

---

## Extensions Beyond the Contract

None in this Windows implementation.  Image builds, Compose
orchestration, registry auth, Docker Desktop / WSL2 distro lifecycle,
Windows-Container vs Linux-Container backend switching, and
Kubernetes integration are intentionally deferred.

---

## Future Considerations

* Surface a `CONTAINERD` engine identifier for raw-containerd /
  `nerdctl` Windows-Container deployments where Docker Desktop is
  not installed.
* Add a strict-read mode that raises
  `WindowsContainerQueryError`.
* Add an engine-availability probe so a missing binary raises
  `WindowsContainerEngineNotFoundError` instead of returning
  `FAILED`.
* Add a Docker Desktop / WSL2 distro / Windows-Containers feature
  status verb.
* Add a registry-login verb that streams the password on stdin.
* Add a Windows-Container vs Linux-Container backend probe (via
  `docker info --format '{{.OSType}}'`) so coordinators can
  validate the backend matches the spec's expectations before
  attempting a create.

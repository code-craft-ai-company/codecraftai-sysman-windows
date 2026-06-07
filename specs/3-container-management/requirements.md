# Requirements

## Introduction

The container management feature provides the
`codecraftai-sysman-windows` package with the ability to pull / remove
images, create / start / stop / remove containers, list and inspect
containers, retrieve container logs, and execute commands inside
running containers on Windows systems.  This spec implements the
container-management contract defined at
`codecraftai-sysman-common/specs/contracts/container-management/contract.md`
for Windows using either the `docker.exe` CLI (default — Docker
Desktop) or the `podman.exe` / `wsl.exe podman` CLI (Podman on
Windows is uncommon and routes through WSL2), selected at
construction time via `WindowsContainerManagerConfig.engine`.

A maintenance coordinator running in an external package drives all
operations by calling into this feature's public interfaces.  The
feature does not decide *which* containers should exist, *which*
images should be pulled, or *what* environment / volume / port
configuration a container should have — it provides the capability to
*manipulate container engine state* reliably, idempotently, and with
full auditability.  All container engine commands are executed
exclusively through the `codecraftai-interfaces` command-runner
abstraction.

Windows hosts the engine in two ways: Docker Desktop runs containers
inside a WSL2 distro (Linux containers) or via the native Windows
containerd backend (Windows containers).  This manager invokes
`docker.exe` directly and is agnostic about which backend Docker
Desktop is configured for.  Container engine installation (Docker
Desktop / Podman MSI / Windows feature enablement for Hyper-V,
WSL2) is NOT in scope here — those live in `package-management` /
manual provisioning.

---

## Requirements

### Requirement 1: Pull an Image

**User Story:** As a maintenance coordinator, I want to pull a
container image by reference, so that a specific image version is
present locally before any container creation.

#### Acceptance Criteria

1. WHEN the coordinator calls `pull_image(ref)` THE SYSTEM SHALL
   dispatch `<engine_prefix> pull <ref>` through the command runner
   with `elevated=True` when configured, where `<engine_prefix>` is
   `["docker.exe"]` for `DOCKER`, `["wsl.exe", "podman"]` for
   `PODMAN_WSL` with `use_wsl_for_podman=True`, or
   `["podman.exe"]` for `PODMAN_WSL` with
   `use_wsl_for_podman=False`.
2. WHEN the reference is already present locally (probed via
   `<engine_prefix> images --filter 'reference=<ref>' --format
   '{{json .}}'`) THE SYSTEM SHALL skip execution and return
   `WindowsContainerActionResult(status=SKIPPED)` UNLESS
   `config.force_pull` is True.
3. WHEN `config.force_pull` is True THE SYSTEM SHALL always invoke
   `<engine_prefix> pull` without consulting the idempotency probe.
4. WHEN the pull succeeds THE SYSTEM SHALL return
   `WindowsContainerActionResult(status=SUCCESS)`.
5. WHEN the pull fails THE SYSTEM SHALL return
   `WindowsContainerActionResult(status=FAILED)`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `WindowsContainerActionResult(status=DRY_RUN)`.

---

### Requirement 2: Remove an Image

**User Story:** As a maintenance coordinator, I want to remove an
image by reference.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_image(ref)` THE SYSTEM SHALL
   dispatch `<engine_prefix> rmi <ref>` with `elevated=True` when
   configured.
2. WHEN the reference is absent locally THE SYSTEM SHALL skip
   execution and return
   `WindowsContainerActionResult(status=SKIPPED)`.
3. WHEN the operation succeeds THE SYSTEM SHALL return
   `WindowsContainerActionResult(status=SUCCESS)`.
4. WHEN the operation fails THE SYSTEM SHALL return
   `WindowsContainerActionResult(status=FAILED)`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `WindowsContainerActionResult(status=DRY_RUN)`.

---

### Requirement 3: List Images

**User Story:** As a maintenance coordinator, I want to enumerate
every image present locally.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_images()` THE SYSTEM SHALL
   dispatch `<engine_prefix> images --format '{{json .}}'` WITHOUT
   elevation.
2. THE SYSTEM SHALL parse the response as one JSON object per line.
3. THE SYSTEM SHALL surface `<none>` repository / tag values as
   `None`.
4. WHEN the engine exits non-zero THE SYSTEM SHALL return an empty
   list.

---

### Requirement 4: Create a Container

**User Story:** As a maintenance coordinator, I want to create a
container from an image with a declared spec.

#### Acceptance Criteria

1. WHEN the coordinator provides a `WindowsContainerSpec` THE SYSTEM
   SHALL dispatch
   `<engine_prefix> create --name <name> [<flags...>] <image>
   [<argv...>]`.
2. THE SYSTEM SHALL pass volume tokens through unchanged.  Windows
   coordinators MUST format Linux-container volumes as
   ``"C:\\host\\path:/container/path"`` and Windows-Container
   volumes as ``"C:\\host:C:\\container"``; the manager does not
   rewrite paths.
3. WHEN a container of the same `name` already exists with the same
   `image` (probed via `<engine_prefix> inspect <name>`) THE SYSTEM
   SHALL skip execution and return
   `WindowsContainerActionResult(status=SKIPPED)`.
4. WHEN the operation succeeds THE SYSTEM SHALL return
   `WindowsContainerActionResult(status=SUCCESS)` populated with
   `container_id` from stdout.
5. WHEN the operation fails THE SYSTEM SHALL return
   `WindowsContainerActionResult(status=FAILED)`.
6. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `WindowsContainerActionResult(status=DRY_RUN)`.

---

### Requirement 5: Start a Container

**User Story:** As a maintenance coordinator, I want to start an
existing container by id or name.

#### Acceptance Criteria

1. WHEN the coordinator calls `start_container(container_id)` THE
   SYSTEM SHALL dispatch `<engine_prefix> start <container_id>`.
2. WHEN the container is already running THE SYSTEM SHALL skip
   execution and return
   `WindowsContainerActionResult(status=SKIPPED)`.
3. WHEN the operation succeeds THE SYSTEM SHALL return
   `WindowsContainerActionResult(status=SUCCESS)`.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `WindowsContainerActionResult(status=DRY_RUN)`.

---

### Requirement 6: Stop a Container

**User Story:** As a maintenance coordinator, I want to stop a
running container with a configurable timeout.

#### Acceptance Criteria

1. WHEN the coordinator calls `stop_container(container_id)` THE
   SYSTEM SHALL dispatch
   `<engine_prefix> stop -t <default_stop_timeout>
   <container_id>`.
2. WHEN the container is already stopped THE SYSTEM SHALL skip
   execution and return
   `WindowsContainerActionResult(status=SKIPPED)`.
3. WHEN the container does not exist THE SYSTEM SHALL skip
   execution and return
   `WindowsContainerActionResult(status=SKIPPED)`.
4. WHEN the operation succeeds THE SYSTEM SHALL return
   `WindowsContainerActionResult(status=SUCCESS)`.
5. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `WindowsContainerActionResult(status=DRY_RUN)`.

---

### Requirement 7: Remove a Container

**User Story:** As a maintenance coordinator, I want to remove a
container by id or name.

#### Acceptance Criteria

1. WHEN the coordinator calls `remove_container(container_id)` THE
   SYSTEM SHALL dispatch `<engine_prefix> rm -f <container_id>`.
2. WHEN no container of that id / name exists THE SYSTEM SHALL skip
   execution and return
   `WindowsContainerActionResult(status=SKIPPED)`.
3. WHEN the operation succeeds THE SYSTEM SHALL return
   `WindowsContainerActionResult(status=SUCCESS)`.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `WindowsContainerActionResult(status=DRY_RUN)`.

---

### Requirement 8: List Containers

**User Story:** As a maintenance coordinator, I want to enumerate
every container.

#### Acceptance Criteria

1. WHEN the coordinator calls `list_containers(all=True)` THE
   SYSTEM SHALL dispatch
   `<engine_prefix> ps -a --format '{{json .}}'`.
2. WHEN the coordinator calls `list_containers(all=False)` THE
   SYSTEM SHALL dispatch
   `<engine_prefix> ps --format '{{json .}}'`.
3. THE SYSTEM SHALL parse the response as one JSON object per line.
4. WHEN the engine exits non-zero THE SYSTEM SHALL return an empty
   list.

---

### Requirement 9: Inspect a Container

**User Story:** As a maintenance coordinator, I want to inspect a
single container by id or name.

#### Acceptance Criteria

1. WHEN the coordinator calls `inspect_container(container_id)` THE
   SYSTEM SHALL dispatch `<engine_prefix> inspect <container_id>`
   WITHOUT elevation.
2. THE SYSTEM SHALL parse the JSON ARRAY response and pick element
   0.
3. THE SYSTEM SHALL return a `WindowsContainerInfo`.
4. WHEN the container does not exist THE SYSTEM SHALL return `None`.

---

### Requirement 10: Get Container Logs

**User Story:** As a maintenance coordinator, I want to fetch the
tail of a container's log.

#### Acceptance Criteria

1. WHEN the coordinator calls
   `get_container_logs(container_id, lines)` THE SYSTEM SHALL
   dispatch
   `<engine_prefix> logs --tail <lines> <container_id>` WITHOUT
   elevation.
2. THE SYSTEM SHALL return a `WindowsContainerLogs` populated with
   `container_id`, `lines`, and `truncated=True`.
3. WHEN `lines` is omitted THE SYSTEM SHALL default to 100.
4. WHEN the engine exits non-zero THE SYSTEM SHALL return an empty
   `WindowsContainerLogs` rather than raise.

---

### Requirement 11: Execute a Command in a Container

**User Story:** As a maintenance coordinator, I want to run a
command inside a running container.

#### Acceptance Criteria

1. WHEN the coordinator calls
   `exec_in_container(container_id, argv)` THE SYSTEM SHALL
   dispatch `<engine_prefix> exec <container_id> <argv...>`.
2. THE SYSTEM SHALL return a `WindowsContainerExecResult`.
3. THE SYSTEM SHALL NOT skip — `exec_in_container` is operational.
4. WHEN dry-run mode is active THE SYSTEM SHALL NOT execute any
   command and SHALL return
   `WindowsContainerExecResult(status=DRY_RUN)`.

---

### Requirement 12: Engine Selection and Elevation Policy

**User Story:** As a maintenance coordinator, I want a single
`WindowsContainerManager` API regardless of whether the host runs
Docker Desktop or Podman through WSL2.

#### Acceptance Criteria

1. WHEN `config.engine` is `WindowsContainerEngine.DOCKER` THE
   SYSTEM SHALL invoke `config.docker_executable` (default
   `docker.exe`) for every verb.
2. WHEN `config.engine` is `WindowsContainerEngine.PODMAN_WSL` AND
   `config.use_wsl_for_podman` is True THE SYSTEM SHALL prefix every
   verb with `["wsl.exe", "podman"]`.
3. WHEN `config.engine` is `WindowsContainerEngine.PODMAN_WSL` AND
   `config.use_wsl_for_podman` is False THE SYSTEM SHALL invoke
   `podman.exe` directly.
4. WHEN `config.elevated` is True THE SYSTEM SHALL pass
   `elevated=True` to the runner for every write verb.  Windows
   Docker Desktop normally runs as the desktop user; the
   `elevated` flag is reserved for rootful socket configurations
   and Windows-Container backends that require an elevated token.
5. WHEN performing read-only operations THE SYSTEM SHALL NEVER
   pass `elevated=True`.
6. WHEN `config.elevated` is False THE SYSTEM SHALL NEVER pass
   `elevated=True`.

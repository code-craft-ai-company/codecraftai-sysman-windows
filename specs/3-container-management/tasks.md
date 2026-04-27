# Implementation Plan — Windows Container Management

Checked items (`[x]`) are implemented and covered by tests.  Unchecked
items (`[ ]`) identify remaining work.

## Module and Models

- [x] 1. Create the `automojo.sysman.windows.containermanagement`
  module.

- [x] 2. Define `WindowsContainerActionStatus` (aliased to the
  shared `featurebase.ActionStatus`).

- [x] 3. Define `WindowsContainerEngine` enum with `DOCKER`
  (default) and `PODMAN_WSL`.

- [x] 4. Define `WindowsContainerManagerConfig` dataclass with
  `engine`, `force_pull`, `default_stop_timeout`,
  `use_wsl_for_podman`, and `docker_executable` extras.

- [x] 5. Define `WindowsContainerActionResult` dataclass with
  `engine` / `container_id` / `image` / `target` / `message`
  extras.

- [x] 6. Define `WindowsContainerExecResult` dataclass with
  `engine` / `container_id` / `command_argv` / `output` extras.

- [x] 7. Define `WindowsImageInfo`, `WindowsContainerSpec`,
  `WindowsContainerInfo`, `WindowsContainerLogs` dataclasses.

- [x] 8. Define `WindowsContainerQueryError`,
  `WindowsContainerEngineNotFoundError`,
  `WindowsContainerNotFoundError` exceptions.

## WindowsContainerManager — Implemented

- [x] 9. `__init__` accepting runner + optional config.

- [x] 10. `pull_image(ref)` — `<engine_prefix> pull <ref>`;
  idempotent via `images --filter`.

- [x] 11. `remove_image(ref)` — `<engine_prefix> rmi <ref>`;
  idempotent via `images --filter`.

- [x] 12. `list_images()` — `<engine_prefix> images --format
  '{{json .}}'`.

- [x] 13. `create_container(spec)` — `<engine_prefix> create
  --name <name> [<flags...>] <image> [<argv...>]`; idempotent via
  `<engine_prefix> inspect <name>`.

- [x] 14. `start_container(container_id)` — `<engine_prefix> start
  <id>`; idempotent.

- [x] 15. `stop_container(container_id)` — `<engine_prefix> stop
  -t <timeout> <id>`; idempotent.

- [x] 16. `remove_container(container_id)` — `<engine_prefix> rm
  -f <id>`; idempotent.

- [x] 17. `list_containers(all=False)` — `<engine_prefix> ps [-a]
  --format '{{json .}}'`.

- [x] 18. `inspect_container(container_id)` — `<engine_prefix>
  inspect <id>` parsing the JSON ARRAY.

- [x] 19. `get_container_logs(container_id, lines=100)` —
  `<engine_prefix> logs --tail <lines> <id>`.

- [x] 20. `exec_in_container(container_id, argv)` —
  `<engine_prefix> exec <id> <argv...>`; never skips.

- [x] 21. `_dispatch_container_write` and `_build_result` helpers.

- [x] 22. `_engine_prefix` helper that returns
  `[docker_executable]` for `DOCKER`, `["wsl.exe", "podman"]` or
  `["podman.exe"]` for `PODMAN_WSL` based on
  `use_wsl_for_podman`.

## WindowsContainerManager — Gaps

- [ ] 23. Strict-read mode that raises
  `WindowsContainerQueryError`.

- [ ] 24. Explicit engine-availability probe that raises
  `WindowsContainerEngineNotFoundError`.

- [ ] 25. `CONTAINERD` engine identifier for raw-containerd /
  `nerdctl` Windows-Container deployments.

- [ ] 26. Docker Desktop / WSL2 distro status probe so coordinators
  can confirm the runtime is healthy before attempting verbs.

- [ ] 27. Surface a `force` parameter on `remove_image`.

- [ ] 28. Spec-equivalence comparison in `create_container`
  (env, volumes, ports, networks, restart policy, working dir,
  user).

- [ ] 29. Registry-login verb (`<engine_prefix> login` with stdin
  password).

- [ ] 30. Windows-Container vs Linux-Container backend probe (via
  `docker info --format '{{.OSType}}'`).

## Tests — Implemented

- [x] 31. `test_pull_image_invokes_engine_pull`
- [x] 32. `test_pull_image_skipped_when_already_present`
- [x] 33. `test_pull_image_dry_run_short_circuits`
- [x] 34. `test_remove_image_invokes_rmi`
- [x] 35. `test_list_images_parses_json_lines`
- [x] 36. `test_create_container_invokes_create_with_spec`
  (covers Windows-style volume token round-trip)
- [x] 37. `test_create_container_skipped_when_name_present_with_same_spec`
- [x] 38. `test_start_container_skipped_when_running`
- [x] 39. `test_stop_container_skipped_when_stopped`
- [x] 40. `test_remove_container_invokes_rm_force`
- [x] 41. `test_list_containers_with_all_includes_stopped`
- [x] 42. `test_inspect_container_parses_json_array`
- [x] 43. `test_get_container_logs_invokes_logs_with_tail`
- [x] 44. `test_exec_in_container_invokes_exec`
- [x] 45. `test_engine_selection_dispatches_to_podman_via_wsl_when_configured`
- [x] 46. `test_engine_selection_uses_native_podman_exe_when_use_wsl_false`
- [x] 47. `test_writes_pass_elevated_per_config`
- [x] 48. `test_reads_never_elevate`
- [x] 49. `test_failed_command_surfaces_stderr`
- [x] 50. Module-level parser helpers
  (`_parse_image_lines`, `_parse_container_ps_lines`,
  `_parse_inspect_first`, `_build_create_argv`).

## Tests — Gaps

- [ ] 51. Digest-pinned-reference idempotency test.
- [ ] 52. Backend-detection test (Linux containers vs Windows
  containers) once the probe lands.
- [ ] 53. Engine-not-found exception test once the explicit probe
  lands.
- [ ] 54. Round-trip test for the second volume-mount form
  (Windows-Container `C:\host:C:\container`) — currently only the
  Linux-Container form is exercised.

## Follow-ups

- [ ] 55. **Cross-OS contract conformance review** against
  `automojo-sysman-common/specs/contracts/container-management/contract.md`.

- [ ] 56. **automojo-* reuse check**: confirm no other feature in
  `automojo-sysman-windows` invokes `docker.exe` / `podman.exe` /
  `wsl.exe` directly for container operations.

## Spec deviations from this implementation

- **Engine identifier set is narrower than the contract.**  The
  cross-OS contract enumerates `PODMAN`, `DOCKER`, `CONTAINERD`,
  `DOCKER_DESKTOP`, and `COLIMA`.  This Windows implementation
  surfaces `DOCKER` and `PODMAN_WSL` only.  `DOCKER_DESKTOP` is
  treated as a flavor of `DOCKER` because the binary is the same.
  `CONTAINERD` (raw `nerdctl` against a containerd socket) is
  reserved for follow-up task 25.  `COLIMA` is macOS-only and not
  applicable.

- **`PODMAN_WSL` rather than `PODMAN`.**  The contract uses
  `PODMAN`; this implementation uses the more specific
  `PODMAN_WSL` because podman on Windows ALWAYS routes through
  WSL2 (either via `wsl.exe podman` or via the native
  `podman.exe` driving its own WSL2 machine).  A future enhancement
  could collapse the two if a non-WSL Windows podman backend
  emerges.

- **`create_container` idempotency is image-only.**  Same as
  Ubuntu / macOS.

- **`stop_container` returns SKIPPED when the container is
  absent.**

- **No strict-read mode.**

- **`get_container_logs.truncated` is always True.**

- **No engine-availability probe on construction.**

- **Volume tokens are pass-through.**  The manager does not rewrite
  `C:\host` to `/mnt/c/host` for WSL2 backends; coordinators that
  bind paths must format them according to the active backend
  (Linux containers vs Windows containers).  Docker Desktop
  handles the WSL2 path translation internally; `wsl podman` does
  not, so coordinators driving `PODMAN_WSL` with
  `use_wsl_for_podman=True` and bind-mounting Windows paths must
  resolve the host path themselves (e.g. `/mnt/c/host` instead of
  `C:\host`).

- **`docker_executable` is configurable but `wsl.exe` and
  `podman.exe` are not.**  The `wsl` and `podman` binaries are
  expected to be on PATH at their canonical names; a future
  enhancement could expose those as config fields too if a
  coordinator needs to point at a non-PATH installation.

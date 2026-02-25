# Implementation Plan

- [ ] 1. Create the `packagemanagement` module file
  - Create `source/packages/automojo/sysman/linux/windows/packagemanagement.py`
  - Add module-level docstring describing purpose and coordinator-driven design
  - _Requirements: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10_

- [ ] 2. Define status enum and result dataclasses
  - Implement `PackageActionStatus` enum with values: `SUCCESS`, `FAILED`, `SKIPPED`, `DRY_RUN`
  - Implement `PackageActionResult` dataclass with fields: `status`, `package_name`,
    `version`, `exit_code`, `stdout`, `stderr`, `command`
  - Implement `PackageInfo` dataclass with fields: `name`, `version`, `release`, `arch`
  - Implement `PackageUpdateInfo` dataclass with fields: `name`, `installed_version`,
    `available_version`, `arch`
  - _Requirements: 1, 2, 3, 4, 5, 6, 7, 8, 9_

- [ ] 3. Define typed exceptions
  - Implement `PackageQueryError` with `exit_code` and `stderr` attributes
  - Implement `PackageFileNotFoundError` with `filepath` attribute
  - _Requirements: 4, 5, 6_

- [ ] 4. Implement `PackageManagerConfig` dataclass
  - Add `dry_run: bool = False` field
  - Add `elevated: bool = False` field
  - _Requirements: 1.6, 2.5, 3.6, 6.5, 7.5, 8.5, 9.5, 10_

- [ ] 5. Implement `PackageManager.__init__` and `query()`
  - Accept `ICommandRunner` and `PackageManagerConfig` as constructor arguments
  - Implement `query(name)` using `rpm -q --queryformat` command
  - Parse rpm output into `PackageInfo`; return `None` when rpm exits 1 (not installed)
  - Raise `PackageQueryError` for any other non-zero exit code
  - _Requirements: 4_

- [ ] 6. Implement `install()`
  - Call `query()` to check current state; return `SKIPPED` if already at target version
  - Build `dnf install -y` command with optional version suffix
  - Honour `dry_run` flag; return `DRY_RUN` result without calling runner
  - Pass `elevated` flag to runner; parse result into `PackageActionResult`
  - _Requirements: 1_

- [ ] 7. Implement `remove()`
  - Call `query()` to check current state; return `SKIPPED` if package is not installed
  - Build `dnf remove -y` command
  - Honour `dry_run` flag
  - Pass `elevated` flag to runner; parse result into `PackageActionResult`
  - _Requirements: 2_

- [ ] 8. Implement `update()`
  - Call `query()` to check current state; return `SKIPPED` if already at target version
  - Build `dnf update -y` or `dnf install -y <name>-<version>` command as appropriate
  - Honour `dry_run` flag
  - Pass `elevated` flag to runner; parse result into `PackageActionResult`
  - _Requirements: 3_

- [ ] 9. Implement `list_updates()`
  - Build `dnf check-update` command
  - Treat exit codes `0` and `100` as success; raise `PackageQueryError` for all others
  - Parse dnf output lines into a list of `PackageUpdateInfo` records
  - _Requirements: 5_

- [ ] 10. Implement `install_local()`
  - Validate that `filepath` exists on disk before calling runner; raise
    `PackageFileNotFoundError` if not found
  - Build `dnf install -y <filepath>` command
  - Honour `dry_run` flag
  - Pass `elevated` flag to runner; parse result into `PackageActionResult`
  - _Requirements: 6_

- [ ] 11. Implement `hold()` and `release_hold()`
  - For `hold()`: query versionlock list to detect existing hold; return `SKIPPED` if present
  - Build `dnf versionlock add <name>` command
  - For `release_hold()`: return `SKIPPED` if no hold exists
  - Build `dnf versionlock delete <name>` command
  - Honour `dry_run` flag on both methods
  - Pass `elevated` flag to runner on both methods
  - _Requirements: 7, 8_

- [ ] 12. Implement `group_install()` and `group_remove()`
  - Build `dnf groupinstall -y "<group>"` and `dnf groupremove -y "<group>"` commands
  - Honour `dry_run` flag on both methods
  - Pass `elevated` flag to runner on both methods
  - Map runner result to `PackageActionResult`
  - _Requirements: 9_

- [ ] 13. Create test module
  - Create `source/testroots/testbase/automojo/tests/sysman/linux/windows/test_packagemanagement.py`
  - Implement `MockCommandRunner` that returns configurable `RunResult` values
  - _Requirements: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10_

- [ ] 14. Write tests for `query()`
  - Test: package installed — returns populated `PackageInfo`
  - Test: package not installed (rpm exits 1) — returns `None`
  - Test: unexpected rpm exit code — raises `PackageQueryError`
  - _Requirements: 4_

- [ ] 15. Write tests for `install()`
  - Test: package not installed — runner called with correct args, returns `SUCCESS`
  - Test: package already installed at requested version — returns `SKIPPED`, runner not called
  - Test: install failure — returns `FAILED` with exit code and stderr
  - Test: dry-run mode — returns `DRY_RUN`, runner not called
  - Test: elevated mode — runner called with `elevated=True`
  - _Requirements: 1_

- [ ] 16. Write tests for `remove()`
  - Test: package installed — runner called, returns `SUCCESS`
  - Test: package not installed — returns `SKIPPED`, runner not called
  - Test: remove failure — returns `FAILED`
  - Test: dry-run mode — returns `DRY_RUN`, runner not called
  - _Requirements: 2_

- [ ] 17. Write tests for `update()`
  - Test: newer version available — runner called, returns `SUCCESS` with new version
  - Test: already at target version — returns `SKIPPED`, runner not called
  - Test: update failure — returns `FAILED`
  - Test: dry-run mode — returns `DRY_RUN`, runner not called
  - _Requirements: 3_

- [ ] 18. Write tests for `list_updates()`
  - Test: exit code 100 with output — returns populated `PackageUpdateInfo` list
  - Test: exit code 0 (no updates) — returns empty list
  - Test: unexpected exit code — raises `PackageQueryError`
  - _Requirements: 5_

- [ ] 19. Write tests for `install_local()`
  - Test: valid file path — runner called with correct args, returns `SUCCESS`
  - Test: non-existent file path — raises `PackageFileNotFoundError` before runner is called
  - Test: install failure — returns `FAILED`
  - Test: dry-run mode — returns `DRY_RUN`, runner not called
  - _Requirements: 6_

- [ ] 20. Write tests for `hold()` and `release_hold()`
  - Test: hold applied — runner called, returns `SUCCESS`
  - Test: hold already exists — returns `SKIPPED`, runner not called
  - Test: release hold — runner called, returns `SUCCESS`
  - Test: no hold to release — returns `SKIPPED`
  - Test: dry-run mode on both — returns `DRY_RUN`, runner not called
  - _Requirements: 7, 8_

- [ ] 21. Write tests for `group_install()` and `group_remove()`
  - Test: group install success — runner called with quoted group name, returns `SUCCESS`
  - Test: group install failure (unknown group) — returns `FAILED` with stderr
  - Test: group remove success — runner called, returns `SUCCESS`
  - Test: dry-run mode on both — returns `DRY_RUN`, runner not called
  - _Requirements: 9_
